"""Batched delivery independent of Athena's websocket and route retention."""
import random
import threading
import json
from email.utils import parsedate_to_datetime
import time

from openpilot.system.vehicle_telemetry.clock import boot_ns
from openpilot.system.vehicle_telemetry.store import Outbox

RECOVERED = threading.Event()


def connection_recovered():
  RECOVERED.set()


def send_batch(body):
  import requests
  from openpilot.common.api import Api
  from openpilot.common.params import Params

  dongle_id = Params().get("DongleId")
  if not dongle_id or dongle_id == "UnregisteredDevice":
    return 401, {}, None
  api = Api(dongle_id)
  # BaseApi.post forwards unknown keywords to the query string. Use the primary
  # API configuration and signer, but send these exact digest-covered bytes.
  with requests.post(f"{api.service.api_host}/v1/devices/{dongle_id}/vehicle-telemetry/batch", data=body,
                     headers={"Authorization": "JWT " + api.get_token(), "Content-Type": "application/json"},
                     timeout=(10, 30), stream=True, allow_redirects=False) as response:
    try:
      raw = bytearray()
      if response.status_code == 200:
        for chunk in response.iter_content(8192):
          raw.extend(chunk)
          if len(raw) > 65536:
            raise ValueError("Acknowledgement exceeds limit")
      result = json.loads(raw) if raw else {}
    except ValueError:
      result = {}
    try:
      retry_after = min(86400, max(0, int(response.headers.get("Retry-After", ""))))
    except ValueError:
      try:
        # An HTTP-date delay is advisory. Clamp conservatively; this must not
        # change measurement time trust or any persisted observation.
        retry_after = min(86400, max(5, parsedate_to_datetime(response.headers.get("Retry-After", "")).timestamp() - time.time_ns() / 1e9))
      except (ValueError, TypeError, OverflowError):
        retry_after = None
    return response.status_code, result, retry_after


class Uploader:
  def __init__(self, store, send=send_batch, clock=boot_ns, random_factor=lambda: random.uniform(0.8, 1.2)):
    self.store, self.send, self.clock, self.random_factor = store, send, clock, random_factor
    self.failures = 0

  def step(self):
    token, rows = self.store.claim(self.clock())
    if not rows:
      return False
    try:
      status, result, retry_after = self.send(self.store.body(rows))
    except Exception:
      status, result, retry_after = 0, {}, None
    results = result.get("results", []) if status == 200 and isinstance(result, dict) else []
    acknowledged = {r.get("record_id") for r in results if isinstance(r, dict) and r.get("status") in ("accepted", "duplicate")
                    and any(row["id"] == r.get("record_id") and row["digest"] == r.get("payload_sha256") for row in rows)}
    self.failures = 0 if len(acknowledged) == len(rows) else min(self.failures + 1, 11)
    attempts = max((row["attempts"] for row in rows), default=0)
    delay = min(3600, max(5, 5 * 2 ** min(max(attempts, self.failures - 1), 10) * self.random_factor()))
    if status in (401, 403, 404, 405, 410):
      delay = 3600
    if retry_after is not None:
      delay = max(delay, retry_after)
    self.store.finish(token, rows, results, self.clock(), delay)
    return bool(acknowledged)


def upload_loop(path, boot, stopped: threading.Event):
  from openpilot.common.swaglog import cloudlog
  while not stopped.is_set():
    store = None
    try:
      store = Outbox(path, boot)
      uploader = Uploader(store)
      while not stopped.is_set():
        if RECOVERED.is_set():
          RECOVERED.clear()
          store.db.execute("UPDATE records SET retry_at=0,attempts=0 WHERE state='pending'")
          uploader.failures = 0
        if not uploader.step():
          stopped.wait(1)
    except Exception:
      cloudlog.exception("vehicle telemetry upload worker failed; queued records retained")
      stopped.wait(5)
    finally:
      if store is not None:
        store.close()
