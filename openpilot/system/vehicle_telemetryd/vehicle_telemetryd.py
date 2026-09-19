#!/usr/bin/env python3
"""Best-effort passive telemetry, intentionally isolated from vehicle controls."""
import copy
import queue
import threading
import time
import uuid

from openpilot.system.vehicle_telemetryd.state import SnapshotStore, telemetry_params, vehicle_identity, write_runtime

UPLOAD_INTERVAL = 30.0
UNSUPPORTED_REPROBE = 3600.0


class LatestUploader:
  """Network worker owns no storage; main thread persists acknowledgements."""
  def __init__(self, send, monotonic=time.monotonic):
    self.send = send
    self.monotonic = monotonic
    self.inbox = queue.Queue(maxsize=1)
    self.acknowledged = queue.Queue()
    self.stopped = threading.Event()
    self.pending = None
    self.next_attempt = 0.0
    self.retry = 5.0

  def submit(self, snapshot):
    try:
      self.inbox.get_nowait()
    except queue.Empty:
      pass
    self.inbox.put_nowait(copy.deepcopy(snapshot))

  def step(self):
    try:
      self.pending = self.inbox.get_nowait()
    except queue.Empty:
      pass
    now = self.monotonic()
    if self.pending is None or now < self.next_attempt:
      return
    snapshot = self.pending
    try:
      status = self.send(snapshot)
    except Exception:
      # No token, disconnected, timeout: keep just the latest state. Never log
      # the request, its identity token, VIN-derived identity or readings.
      status = 0
    if 200 <= status < 300:
      self.acknowledged.put(snapshot)
      self.pending = None
      self.retry = 5.0
      self.next_attempt = 0.0
    elif status in (404, 405, 410):
      self.next_attempt = now + UNSUPPORTED_REPROBE
    else:
      self.next_attempt = now + self.retry
      self.retry = min(self.retry * 2, 300.0)

  def run(self):
    while not self.stopped.is_set():
      self.step()
      self.stopped.wait(0.5)


def send_snapshot(snapshot):
  from openpilot.common.api import Api
  from openpilot.common.params import Params
  dongle_id = Params().get("DongleId")
  if not dongle_id or dongle_id == "UnregisteredDevice":
    return 401
  api = Api(dongle_id)  # Primary API_HOST and device JWT; never Sunnylink.
  response = api.post(f"v1/devices/{dongle_id}/vehicle-state", json=snapshot,
                      access_token=api.get_token(), timeout=10)
  try:
    return response.status_code
  finally:
    response.close()


def read_current_car_params(params):
  # Manager clears CarParams before publishing IsOffroad=False, then starts
  # card. Reading only after that publication avoids the previous trip cache.
  if params.get("IsOffroad") is not False:
    return None
  value = params.get("CarParams")
  return value if params.get("IsOffroad") is False else None


def main():
  import openpilot.cereal.messaging as messaging
  from opendbc.car.structs import car
  from openpilot.common.params import Params
  from openpilot.common.swaglog import cloudlog
  from openpilot.system.vehicle_telemetryd.adapters import adapter_for

  params = Params()
  # Clear stale runtime before loading durable state; corrupt durable ordering
  # must fail closed rather than restart from a possibly reused sequence.
  write_runtime({"snapshot": None, "heartbeat": time.monotonic(), "last_frame": None})
  store = SnapshotStore(telemetry_params(), time.time())  # noqa: TID251 - persisted ordering/observations require UTC, retries use monotonic.
  uploader = LatestUploader(send_snapshot)
  upload_thread = threading.Thread(target=uploader.run, name="vehicle_telemetry_upload", daemon=True)
  upload_thread.start()
  resumed_pending = False
  can_socket = messaging.sub_sock("can", timeout=0)
  onroad_previous = False
  current_cp = None
  identity = fingerprint = None
  adapter = None
  trip_id = str(uuid.uuid4())
  next_publish = 0.0
  last_persist = 0.0
  first_reading_pending = True
  samples = {}

  try:
    while True:
      mono, wall = time.monotonic(), time.time()  # noqa: TID251 - wire observations are UTC timestamps.
      onroad = params.get("IsOffroad") is False
      if not resumed_pending and params.get("IsOffroad") is True:
        if store.snapshot is not None and store.snapshot["onroad"]:
          previous = store.snapshot
          store.publish(previous["vehicle_id"], previous["vehicle_fingerprint"], previous["vehicle_supported"], False, {}, wall)
          store.queue_latest()
        if store.pending is not None:
          uploader.submit(store.pending)
        resumed_pending = True
      if onroad != onroad_previous:
        if not onroad and current_cp is not None:
          store.publish(identity, fingerprint, adapter is not None, False, samples, wall)
          store.queue_latest()
          uploader.submit(store.pending)
        current_cp = None
        adapter = None
        identity = fingerprint = None
        samples = {}
        trip_id = str(uuid.uuid4())
        first_reading_pending = True
        onroad_previous = onroad
        next_publish = 0.0

      cp_bytes = read_current_car_params(params) if onroad else None
      if cp_bytes is not None and cp_bytes != current_cp:
        if current_cp is not None:
          trip_id = str(uuid.uuid4())
        with car.CarParams.from_bytes(cp_bytes) as CP:
          identity = vehicle_identity(CP, trip_id)
          fingerprint = CP.carFingerprint or "unknown"
          adapter = adapter_for(CP)
        current_cp = cp_bytes
        samples = {}
        first_reading_pending = True
        # Immediately clear incompatible cache, including explicit unsupported
        # cars. No unsupported snapshot is invented while awaiting CarParams.
        store.publish(identity, fingerprint, adapter is not None, True, {}, wall)
        store.queue_latest()
        uploader.submit(store.pending)
        resumed_pending = True
        last_persist = mono
        next_publish = mono

      packets = [(msg.logMonoTime, [(frame.address, frame.dat, frame.src) for frame in msg.can])
                 for msg in messaging.drain_sock(can_socket)]
      # Frames can arrive during Params reads and socket draining. Sample the
      # clock afterwards so those frames are not discarded as being future.
      mono, wall = time.monotonic(), time.time()  # noqa: TID251 - wire observations are UTC timestamps.
      if adapter is not None and onroad:
        samples.update(adapter.update(packets, mono, wall))

      if mono >= next_publish:
        if current_cp is not None and onroad:
          store.publish(identity, fingerprint, adapter is not None, True, samples, wall)
          if (first_reading_pending and samples) or mono - last_persist >= UPLOAD_INTERVAL:
            store.queue_latest()
            uploader.submit(store.pending)
            last_persist = mono
            if samples:
              first_reading_pending = False
        write_runtime({"snapshot": store.snapshot, "heartbeat": mono,
                       "last_frame": adapter.last_frame if adapter is not None else None})
        next_publish = mono + 1.0

      while True:
        try:
          store.acknowledge(uploader.acknowledged.get_nowait())
        except queue.Empty:
          break
      time.sleep(0.1)
  except Exception:
    cloudlog.exception("vehicle_telemetryd failed; collection and durable ordering will be revalidated on restart")
    raise
  finally:
    uploader.stopped.set()
    uploader_thread_timeout = 11
    upload_thread.join(timeout=uploader_thread_timeout)


if __name__ == "__main__":
  main()
