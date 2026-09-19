import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpilot.system.vehicle_telemetryd.state import (
  DURABLE_KEY, SnapshotStore, get_vehicle_state, runtime_path, telemetry_params, valid_snapshot, vehicle_identity, write_runtime,
)
from openpilot.system.vehicle_telemetryd.vehicle_telemetryd import LatestUploader, read_current_car_params


class FakeParams:
  def __init__(self, value=None):
    self.values = {} if value is None else {DURABLE_KEY: copy.deepcopy(value)}
    self.writes = []

  def get(self, key):
    return copy.deepcopy(self.values.get(key))

  def put(self, key, value, block=False):
    self.values[key] = copy.deepcopy(value)
    self.writes.append((key, copy.deepcopy(value), block))


class TestSnapshots(unittest.TestCase):
  def setUp(self):
    self.params = FakeParams()
    self.store = SnapshotStore(self.params, 1000)

  def publish(self, identity="vin:one", supported=True, onroad=True, samples=None, wall=1001):
    return self.store.publish(identity, "AUDI_A3_MK3", supported, onroad, samples or {}, wall)

  def test_epoch_persisted_before_publication_and_restart(self):
    self.assertEqual(self.params.get(DURABLE_KEY)["collector_epoch"], 1000000)
    first = self.publish(samples={"fuel_percent": (0, 1001, "test")})
    self.store.queue_latest()
    restarted = SnapshotStore(self.params, 900)
    self.assertEqual(restarted.epoch, 1000001)
    self.assertEqual(restarted.pending, first)
    newer = restarted.publish("vin:one", "AUDI_A3_MK3", True, True, {}, 1002)
    self.assertEqual(newer["snapshot_seq"], 1)
    self.assertEqual(newer["metrics"]["fuel_percent"]["value"], 0)

  def test_first_received_zero_independent_timestamps_and_no_refresh_retimestamp(self):
    before = self.publish()
    self.assertFalse(before["metrics"]["fuel_percent"]["supported"])
    first = self.publish(samples={"fuel_percent": (0, 1001, "percent"), "odometer_km": (17000, 1000, "odo")})
    next_snapshot = self.publish(wall=1005)
    self.assertEqual(first["metrics"], next_snapshot["metrics"])
    self.assertNotEqual(first["snapshot_seq"], next_snapshot["snapshot_seq"])
    self.assertNotEqual(first["metrics"]["odometer_km"]["observed_at"], first["metrics"]["fuel_percent"]["observed_at"])

  def test_vehicle_change_and_unsupported_clear_readings(self):
    self.publish(samples={"fuel_percent": (50, 1001, "test")})
    other = self.publish(identity="vin:two")
    self.assertFalse(any(m["supported"] for m in other["metrics"].values()))
    self.publish(identity="vin:two", samples={"odometer_km": (22, 1001, "test")})
    unsupported = self.publish(identity="vin:two", supported=False)
    self.assertFalse(unsupported["vehicle_supported"])
    self.assertFalse(any(m["supported"] for m in unsupported["metrics"].values()))

  def test_acknowledging_older_upload_cannot_delete_newer_pending(self):
    old = self.publish()
    self.store.queue_latest()
    new = self.publish(wall=1002)
    self.store.queue_latest()
    self.store.acknowledge(old)
    self.assertEqual(self.store.pending, new)
    self.store.acknowledge(new)
    self.assertIsNone(self.store.pending)
    self.assertEqual(self.params.get(DURABLE_KEY)["snapshot"], new)

  def test_corrupt_persistence_is_not_overwritten(self):
    for corrupt in ([], {"collector_epoch": 0}, {"collector_epoch": True}, {"collector_epoch": 42, "snapshot": {}}):
      with self.subTest(corrupt=corrupt):
        params = FakeParams(corrupt)
        with self.assertRaises(ValueError):
          SnapshotStore(params, 1000)
        self.assertEqual(params.writes, [])

  def test_failed_epoch_write_stops_publication(self):
    self.params.put = lambda *args, **kwargs: None
    with self.assertRaises(RuntimeError):
      SnapshotStore(self.params, 1000)

  def test_clock_correction_drops_future_readings_without_changing_old_snapshot(self):
    old = self.publish(samples={"fuel_percent": (20, 1001, "test")})
    corrected = self.publish(wall=900)
    self.assertTrue(valid_snapshot(corrected))
    self.assertTrue(corrected["metrics"]["fuel_percent"]["supported"])
    self.assertIsNone(corrected["metrics"]["fuel_percent"]["value"])
    self.assertEqual(old["metrics"]["fuel_percent"]["value"], 20)

  def test_malformed_and_sentinel_snapshot_rejected(self):
    valid = self.publish(samples={"fuel_percent": (0, 1001, "test")})
    self.assertTrue(valid_snapshot(valid))
    for bad in (float("nan"), -1, 101, True, "0"):
      snapshot = copy.deepcopy(valid)
      snapshot["metrics"]["fuel_percent"]["value"] = bad
      self.assertFalse(valid_snapshot(snapshot))
    snapshot = copy.deepcopy(valid)
    snapshot["metrics"]["fuel_percent"]["supported"] = False
    self.assertFalse(valid_snapshot(snapshot))

  def test_identity_is_vin_hash_or_trip_specific_and_never_contains_vin(self):
    cp = SimpleNamespace(carVin="WAUZZZ8V0LA000001", carFingerprint="AUDI_A3_MK3")
    first = vehicle_identity(cp, "trip1")
    self.assertEqual(first, vehicle_identity(cp, "trip2"))
    self.assertNotIn(cp.carVin, first)
    cp.carVin = "00000000000000000"
    self.assertNotEqual(vehicle_identity(cp, "trip1"), vehicle_identity(cp, "trip2"))
    cp.carVin = "invalid"
    self.assertEqual(vehicle_identity(cp, "trip2"), "trip:trip2")

  def test_getter_current_stale_offroad_and_durable_fallback(self):
    with tempfile.TemporaryDirectory() as root, patch.dict("os.environ", {"VEHICLE_TELEMETRY_RUNTIME_ROOT": root}):
      snapshot = self.publish(samples={"fuel_percent": (0, 1001, "test")})
      self.store.queue_latest()
      write_runtime({"snapshot": snapshot, "heartbeat": 100, "last_frame": 99})
      self.assertEqual(get_vehicle_state(self.params, 102)["availability"], "current")
      write_runtime({"snapshot": snapshot, "heartbeat": 110, "last_frame": 99})
      self.assertEqual(get_vehicle_state(self.params, 111)["availability"], "last_known")
      # Stale daemon and corrupt runtime fall back to durable data, never current.
      self.assertEqual(get_vehicle_state(self.params, 120)["availability"], "last_known")
      runtime_path().write_text("{bad")
      self.assertEqual(get_vehicle_state(self.params, 120)["availability"], "last_known")
      offroad = self.publish(onroad=False)
      write_runtime({"snapshot": offroad, "heartbeat": 110, "last_frame": 110})
      self.assertEqual(get_vehicle_state(self.params, 111)["availability"], "last_known")
      unsupported = self.publish(supported=False)
      write_runtime({"snapshot": unsupported, "heartbeat": 110, "last_frame": 110})
      self.assertEqual(get_vehicle_state(self.params, 111)["availability"], "unsupported")
      self.assertEqual(json.loads(runtime_path().read_text())["snapshot"], unsupported)

  def test_corrupt_durable_no_runtime_returns_unknown(self):
    with tempfile.TemporaryDirectory() as root, patch.dict("os.environ", {"VEHICLE_TELEMETRY_RUNTIME_ROOT": root}):
      self.assertEqual(get_vehicle_state(FakeParams({"snapshot": {}})), {"snapshot": None, "availability": "unknown"})

  def test_current_car_params_never_uses_persistent_cache(self):
    self.params.values.update({"CarParamsPersistent": b"old", "CarParams": b"new", "IsOffroad": True})
    self.assertIsNone(read_current_car_params(self.params))
    self.params.values["IsOffroad"] = False
    self.assertEqual(read_current_car_params(self.params), b"new")
    del self.params.values["CarParams"]
    self.assertIsNone(read_current_car_params(self.params))

  def test_onroad_transition_race_discards_read(self):
    class RacingParams:
      def __init__(self):
        self.reads = 0

      def get(self, key):
        self.reads += 1
        return False if self.reads == 1 else b"prior" if key == "CarParams" else True

    self.assertIsNone(read_current_car_params(RacingParams()))


class TestUpload(unittest.TestCase):
  def test_authentication_can_recover_without_waiting_for_unsupported_reprobe(self):
    uploader = LatestUploader(lambda _: 401, lambda: 100)
    uploader.submit({"snapshot_seq": 1})
    uploader.step()
    self.assertEqual(uploader.next_attempt, 105)

  def test_corrected_snapshot_after_clock_rejection_uses_bounded_retry(self):
    for rejected in (400, 422):
      with self.subTest(status=rejected):
        now = [100]
        sent = []

        def send(snapshot, submitted=sent, status=rejected):
          submitted.append(snapshot)
          return status if len(submitted) == 1 else 200

        uploader = LatestUploader(send, lambda clock=now: clock[0])
        invalid_clock = {"collector_epoch": 1000, "snapshot_seq": 1, "generated_at": "2026-09-20T12:00:00.000Z"}
        corrected = {"collector_epoch": 1000, "snapshot_seq": 2, "generated_at": "2026-09-19T12:00:00.000Z"}
        uploader.submit(invalid_clock)
        uploader.step()
        self.assertEqual(uploader.next_attempt, 105)
        uploader.submit(corrected)
        now[0] = 105
        uploader.step()
        self.assertEqual(sent, [invalid_clock, corrected])
        self.assertEqual(uploader.acknowledged.get_nowait(), corrected)

  def test_latest_only_transient_retry_preserves_timestamps(self):
    now = [100]
    sent = []

    def send(snapshot):
      sent.append(snapshot)
      return 503 if len(sent) == 1 else 200

    uploader = LatestUploader(send, lambda: now[0])
    first = {"snapshot_seq": 1, "generated_at": "original"}
    latest = {"snapshot_seq": 2, "generated_at": "new"}
    uploader.submit(first)
    uploader.step()
    self.assertEqual(uploader.next_attempt, 105)
    uploader.submit(latest)
    uploader.step()
    self.assertEqual(len(sent), 1)
    now[0] = 105
    uploader.step()
    self.assertEqual(sent, [first, latest])
    self.assertEqual(uploader.acknowledged.get_nowait(), latest)

  def test_backoff_caps_and_unsupported_server_is_not_reprobed_on_each_snapshot(self):
    now = [100]
    uploader = LatestUploader(lambda _: 500, lambda: now[0])
    uploader.submit({"snapshot_seq": 1})
    for delay in (5, 10, 20, 40, 80, 160, 300, 300):
      uploader.step()
      self.assertEqual(uploader.next_attempt, now[0] + delay)
      now[0] = uploader.next_attempt
    uploader.send = lambda _: 404
    uploader.step()
    next_probe = uploader.next_attempt
    self.assertEqual(next_probe, now[0] + 3600)
    uploader.submit({"snapshot_seq": 2})
    now[0] += 30
    uploader.step()
    self.assertEqual(uploader.next_attempt, next_probe)


class TestPrebuiltStorage(unittest.TestCase):
  def test_isolated_root_and_narrow_key_override_support_prebuilt_maps(self):
    class PrebuiltParams:
      def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

      def check_key(self, key):
        raise KeyError("not in compiled key map")

      def get_type(self, key):
        raise KeyError("not in compiled key map")

      def get_param_path(self, key):
        return str(self.root / key)

      def put(self, key, value, block=False):
        self.check_key(key)
        assert self.get_type(key) == 5
        Path(self.get_param_path(key)).write_text(json.dumps(value))

    native_stub = SimpleNamespace(Params=PrebuiltParams, ParamKeyType=SimpleNamespace(JSON=5), UnknownKeyName=KeyError,
                                  ensure_bytes=lambda key: key.encode() if isinstance(key, str) else key)
    with tempfile.TemporaryDirectory() as root, patch.dict("sys.modules", {"openpilot.common.params": native_stub}):
      params = telemetry_params(str(Path(root) / "private_telemetry"))
      self.assertEqual(params.check_key(DURABLE_KEY), DURABLE_KEY.encode())
      self.assertEqual(params.get_type(DURABLE_KEY), 5)
      with self.assertRaises(KeyError):
        params.check_key("CarParams")
      with self.assertRaises(KeyError):
        params.check_key(DURABLE_KEY + "\x00")
      self.assertIsNone(params.get(DURABLE_KEY))
      store = SnapshotStore(params, 1000)
      self.assertEqual(params.get(DURABLE_KEY)["collector_epoch"], store.epoch)
      self.assertEqual(list(Path(root).iterdir()), [Path(root) / "private_telemetry"])
      for corrupt in ("", "{bad", "null", "[]"):
        Path(params.get_param_path(DURABLE_KEY)).write_text(corrupt)
        with self.assertRaises(ValueError):
          SnapshotStore(params, 1001)
        self.assertEqual(Path(params.get_param_path(DURABLE_KEY)).read_text(), corrupt)


class TestCollectorLifecycle(unittest.TestCase):
  def test_parked_restart_first_frame_offroad_and_vehicle_change(self):
    from opendbc.can import CANPacker
    from opendbc.car.structs import car
    from openpilot.system.vehicle_telemetryd import vehicle_telemetryd as daemon

    cp = car.CarParams.new_message(brand="volkswagen", carFingerprint="AUDI_A3_MK3", carVin="0" * 17,
                                   notCar=False, networkLocation="gateway", safetyConfigs=[{}]).to_bytes()
    unknown = car.CarParams.new_message(brand="mock", carFingerprint="MOCK", notCar=True).to_bytes()
    road = [False, True, True, True, False, True, True]
    cp_values = [None, None, cp, cp, cp, cp, unknown]
    step = [0]
    normal = FakeParams()
    private = FakeParams()
    old = SnapshotStore(private, 900)
    old.publish("trip:previous", "AUDI_A3_MK3", True, True, {"fuel_percent": (50, 900, "test")}, 900)
    old.queue_latest()
    envelopes = []
    params_get = normal.get

    def get(key):
      if key == "IsOffroad":
        return not road[step[0]]
      if key == "CarParams":
        return cp_values[step[0]]
      return params_get(key)

    normal.get = get
    packer = CANPacker("vw_mqb")
    frame = packer.make_can_msg("Kombi_02", 0, {"KBI_Kilometerstand": 50000, "KBI_Inhalt_Tank": 25})

    def drain(_):
      frames = [SimpleNamespace(address=frame[0], dat=frame[1], src=frame[2])]
      return [SimpleNamespace(logMonoTime=int((100 + step[0]) * 1e9), can=frames)] if step[0] == 2 else []

    def advance(_):
      step[0] += 1
      if step[0] == len(road):
        raise KeyboardInterrupt

    messaging = SimpleNamespace(sub_sock=lambda *args, **kwargs: None, drain_sock=drain)
    modules = {"openpilot.cereal": SimpleNamespace(messaging=messaging), "openpilot.cereal.messaging": messaging,
               "openpilot.common.params": SimpleNamespace(Params=lambda: normal),
               "openpilot.common.swaglog": SimpleNamespace(cloudlog=SimpleNamespace(exception=lambda _: None))}
    with patch.dict("sys.modules", modules), patch.object(daemon, "telemetry_params", return_value=private), \
         patch.object(daemon, "write_runtime", side_effect=lambda value: envelopes.append(copy.deepcopy(value))), \
         patch.object(daemon.threading, "Thread"), patch.object(daemon.time, "monotonic", side_effect=lambda: 100 + step[0]), \
         patch.object(daemon.time, "time", side_effect=lambda: 1000 + step[0]), patch.object(daemon.time, "sleep", side_effect=advance):
      with self.assertRaises(KeyboardInterrupt):
        daemon.main()
    published = [item["snapshot"] for item in envelopes if item["snapshot"] is not None]
    self.assertFalse(published[0]["onroad"])
    self.assertEqual(published[0]["metrics"]["fuel_percent"]["observed_at"], old.snapshot["metrics"]["fuel_percent"]["observed_at"])
    first_drive = published[2]
    self.assertEqual(first_drive["metrics"]["odometer_km"]["value"], 50000)
    self.assertFalse(published[4]["onroad"])
    self.assertNotEqual(published[5]["vehicle_id"], first_drive["vehicle_id"])
    self.assertFalse(published[5]["metrics"]["odometer_km"]["supported"])
    self.assertFalse(published[-1]["vehicle_supported"])
    self.assertFalse(private.get(DURABLE_KEY)["pending"]["vehicle_supported"])


class TestMQBAdapter(unittest.TestCase):
  def setUp(self):
    from opendbc.can import CANPacker
    from opendbc.car.structs import car
    from openpilot.system.vehicle_telemetryd.adapters import adapter_for
    self.cp = car.CarParams.new_message(brand="volkswagen", carFingerprint="AUDI_A3_MK3", notCar=False,
                                       networkLocation="gateway", safetyConfigs=[{}])
    self.packer = CANPacker("vw_mqb")
    self.adapter = adapter_for(self.cp)

  def update(self, message, values, mono=100, bus=0):
    frame = self.packer.make_can_msg(message, bus, values)
    return self.adapter.update([(int(mono * 1e9), [frame])], mono, 1000 + mono)

  def test_missing_frames_never_produce_defaults_and_zero_is_valid(self):
    self.assertEqual(self.adapter.update([], 100, 1100), {})
    result = self.update("Kombi_02", {"KBI_Kilometerstand": 0, "KBI_Inhalt_Tank": 0})
    self.assertEqual(result["odometer_km"][0], 0)
    self.assertEqual(result["fuel_liters"][0], 0)
    self.assertNotIn("fuel_percent", result)

  def test_real_mqb_packed_signals_and_high_resolution_precedence(self):
    self.update("Kombi_02", {"KBI_Kilometerstand": 45000, "KBI_Inhalt_Tank": 37})
    result = self.update("Kombi_03", {"KBI_Tankinhalt_hochaufl": 36.42, "KBI_Tankfuellstand_Prozent": 65})
    self.assertEqual(result["odometer_km"][0], 45000)
    self.assertAlmostEqual(result["fuel_liters"][0], 36.42)
    self.assertEqual(result["fuel_percent"][0], 65)
    self.assertTrue(result["fuel_liters"][2].endswith("KBI_Tankinhalt_hochaufl"))

  def test_error_sentinels_fallback_and_stale_samples(self):
    self.update("Kombi_02", {"KBI_Kilometerstand": 1048575, "KBI_Inhalt_Tank": 37})
    result = self.update("Kombi_03", {"KBI_Tankinhalt_hochaufl": 163.83, "KBI_Tankfuellstand_Prozent": 127})
    self.assertEqual(result["fuel_liters"][0], 37)
    self.assertNotIn("fuel_percent", result)
    self.assertNotIn("odometer_km", result)
    self.assertEqual(self.adapter.update([], 111, 1111), {})

  def test_init_sentinels_and_range_validation(self):
    result = self.update("Kombi_02", {"KBI_Kilometerstand": 1048574, "KBI_Inhalt_Tank": 126})
    self.assertEqual(result, {})
    result = self.update("Kombi_03", {"KBI_Tankinhalt_hochaufl": 163.82, "KBI_Tankfuellstand_Prozent": 126})
    self.assertEqual(result, {})

  def test_tank_fault_rejects_coarse_liters_without_discarding_odometer(self):
    result = self.update("Kombi_02", {"KBI_Kilometerstand": 50000, "KBI_Inhalt_Tank": 30, "KBI_FStatus_Tank": 1})
    self.assertNotIn("fuel_liters", result)
    self.assertEqual(result["odometer_km"][0], 50000)

  def test_bus_offsets_and_no_camera_bus_fallback(self):
    from openpilot.system.vehicle_telemetryd.adapters import adapter_for
    self.cp.safetyConfigs = [{}, {}]
    self.adapter = adapter_for(self.cp)
    self.assertEqual(self.adapter.parser.bus, 4)
    self.assertEqual(self.update("Kombi_02", {"KBI_Kilometerstand": 42}, bus=0), {})
    self.assertEqual(self.update("Kombi_02", {"KBI_Kilometerstand": 42}, bus=6), {})
    self.assertEqual(self.update("Kombi_02", {"KBI_Kilometerstand": 42}, bus=4)["odometer_km"][0], 42)

  def test_truncated_or_queued_old_packets_cannot_manufacture_zero(self):
    self.assertEqual(self.adapter.update([(100_000_000_000, [(0x6B7, b"\x00", 0)])], 100, 1100), {})
    frame = self.packer.make_can_msg("Kombi_02", 0, {"KBI_Kilometerstand": 42})
    self.assertEqual(self.adapter.update([(80_000_000_000, [frame])], 100, 1100), {})

  def test_unknown_other_platform_and_not_car_are_unsupported(self):
    from openpilot.system.vehicle_telemetryd.adapters import adapter_for
    for fingerprint in ("UNKNOWN", "VOLKSWAGEN_PASSAT_NMS", "VOLKSWAGEN_ID4_MK1"):
      self.cp.carFingerprint = fingerprint
      self.assertIsNone(adapter_for(self.cp))
    self.cp.carFingerprint = "AUDI_A3_MK3"
    self.cp.notCar = True
    self.assertIsNone(adapter_for(self.cp))

  def test_optional_parser_does_not_change_driving_parser(self):
    from opendbc.can import CANParser
    driving_parser = CANParser("vw_mqb", [], 0)
    before = copy.deepcopy(driving_parser.addresses)
    self.update("Kombi_02", {"KBI_Kilometerstand": 42})
    self.assertEqual(driving_parser.addresses, before)
    self.assertNotEqual(self.adapter.parser.addresses, before)


if __name__ == "__main__":
  unittest.main()
