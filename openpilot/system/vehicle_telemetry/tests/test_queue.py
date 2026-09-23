import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from openpilot.system.vehicle_telemetry.clock import NANO, MessageClock, TrustedClock, python_to_boot_ns
from openpilot.system.vehicle_telemetry.collector import Collector, current_segment
from openpilot.system.vehicle_telemetry.store import BYTE_LIMIT, Outbox, Staging
from openpilot.system.vehicle_telemetry.uploader import Uploader

UTC = 1_800_000_000 * NANO
BOOT = "11111111-1111-4111-8111-111111111111"


def trusted():
  clock = TrustedClock()
  clock.observe("gnss", 10 * NANO, UTC, 10 * NANO)
  clock.observe("gnss", 11 * NANO, UTC + NANO, 11 * NANO)
  return clock


class TestClock(unittest.TestCase):
  def test_plausible_wall_clock_is_not_trusted(self):
    clock = TrustedClock()
    clock.check_wall(NANO, UTC)
    self.assertIsNone(clock.anchor)
    self.assertFalse(clock.observe("gnss", 10 * NANO, UTC, 10 * NANO))
    self.assertIsNone(clock.anchor)

  def test_high_frequency_gnss_and_interleaved_ntp_anchor(self):
    clock = TrustedClock()
    for number in range(11):
      measured = 10 * NANO + number * 100_000_000
      clock.observe("gnss", measured, UTC + number * 100_000_000, measured)
      clock.observe("ntp", measured, UTC + number * 100_000_000, measured)
    self.assertEqual(clock.anchor[0], "gnss")
    self.assertEqual(clock.utc(9 * NANO), UTC / NANO - 1)

  def test_ntp_requires_two_spaced_observations(self):
    clock = TrustedClock()
    self.assertFalse(clock.observe("ntp", NANO, UTC, NANO))
    self.assertFalse(clock.observe("ntp", 2 * NANO, UTC + NANO, 2 * NANO))
    self.assertTrue(clock.observe("ntp", 6 * NANO, UTC + 5 * NANO, 6 * NANO))

  def test_frozen_gnss_timestamp_never_anchors(self):
    clock = TrustedClock()
    for elapsed in range(10):
      self.assertFalse(clock.observe("gnss", elapsed * NANO, UTC, elapsed * NANO))
    self.assertIsNone(clock.anchor)

  def test_wall_steps_in_either_direction_remove_trust(self):
    for change in (-10 * NANO, 10 * NANO):
      clock = trusted()
      clock.check_wall(10 * NANO, UTC)
      self.assertTrue(clock.check_wall(11 * NANO, UTC + NANO + change))
      self.assertIsNone(clock.anchor)

  def test_stale_inconsistent_and_implausible_probes_rejected(self):
    clock = TrustedClock()
    self.assertFalse(clock.observe("gnss", NANO, UTC, 4 * NANO))
    self.assertFalse(clock.observe("ntp", NANO, 1, NANO))
    clock.observe("gnss", NANO, UTC, NANO)
    self.assertFalse(clock.observe("gnss", 2 * NANO, UTC + 100 * NANO, 2 * NANO))
    self.assertIsNone(clock.anchor)

  def test_suspend_offsets_python_messages_but_not_native_can(self):
    self.assertEqual(python_to_boot_ns(9 * NANO, 110 * NANO, 10 * NANO), 109 * NANO)
    self.assertEqual(trusted().utc(12 * NANO) - trusted().utc(11 * NANO), 1)

  def test_suspend_drops_pre_suspend_python_backlog_before_using_new_offset(self):
    clock = MessageClock()
    self.assertEqual(clock.normalize(9 * NANO, 10 * NANO, 10 * NANO, 10 * NANO), 9 * NANO)
    self.assertIsNone(clock.normalize(10 * NANO, 11 * NANO, 111 * NANO, 11 * NANO))
    self.assertEqual(clock.normalize(12 * NANO, 12 * NANO, 112 * NANO, 12 * NANO), 112 * NANO)
    self.assertIsNone(clock.normalize(13 * NANO, 13 * NANO, 113 * NANO, 13 * NANO + 100_000_000))

  def test_startup_baseline_rejects_first_gps_backlog_after_suspend(self):
    clock = MessageClock()
    clock.normalize(0, 8 * NANO, 8 * NANO, 8 * NANO)
    self.assertIsNone(clock.normalize(9 * NANO, 10 * NANO, 110 * NANO, 10 * NANO))


class TestQueue(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.root = Path(self.temp.name)
    self.db = Outbox(self.root / "outbox.db", BOOT, {"collector_epoch": 1_800_000_000_000, "snapshot": None})
    self.stage = Staging(self.root / "volatile.db", BOOT)
    self.collector = Collector(self.db, self.stage, TrustedClock(), BOOT)
    self.collector.select_vehicle("vin:test", "AUDI_A3_MK3", True)

  def tearDown(self):
    self.stage.db.close()
    self.db.close()
    self.temp.cleanup()

  def collect(self, segment=0):
    self.collector.select_segment(("route", segment), 10 * NANO)
    self.collector.samples = {"fuel_percent": (0, 10 * NANO, "percent"), "odometer_km": (17000, 9 * NANO, "odometer")}
    self.collector.checkpoint(11 * NANO)
    self.collector.select_segment(None, 12 * NANO)

  def make_payload(self):
    self.collector.clock = trusted()
    self.collect()
    row = self.db.db.execute("SELECT * FROM records").fetchone()
    return json.loads(row["payload"])

  def test_untrusted_stage_corrected_later_preserves_measurement_times_and_zero(self):
    self.collect()
    self.assertEqual(self.db.db.execute("SELECT count(*) FROM records").fetchone()[0], 0)
    self.assertIsNone(self.collector.publish(20 * NANO, False)["snapshot"])
    self.collector.clock = trusted()
    self.collector.flush()
    self.assertEqual(self.stage.records(), [])
    row = self.db.db.execute("SELECT * FROM records").fetchone()
    value = json.loads(row["payload"])["snapshot"]
    self.assertEqual(value["metrics"]["fuel_percent"]["value"], 0)
    self.assertNotEqual(value["metrics"]["fuel_percent"]["observed_at"], value["metrics"]["odometer_km"]["observed_at"])
    self.assertEqual(hashlib.sha256(row["payload"].encode()).hexdigest(), row["digest"])

  def test_first_supported_vehicle_without_utc_is_unknown(self):
    self.assertEqual(self.collector.publish(10 * NANO, True)["availability"], "unknown")
    self.assertFalse(self.db.meta("invalidated", False))

  def test_restart_preserves_compatible_cached_readings_without_faking_current_frames(self):
    payload = self.make_payload()
    original = payload["snapshot"]["metrics"]
    self.db.save_latest(payload["snapshot"])
    restarted = Collector(self.db, self.stage, trusted(), BOOT)
    restarted.select_vehicle("vin:test", "AUDI_A3_MK3", True)
    envelope = restarted.publish(20 * NANO, True)
    self.assertEqual(envelope["availability"], "last_known")
    self.assertEqual(envelope["snapshot"]["metrics"], original)

  def test_worker_restart_recovers_unresolved_but_reboot_discards(self):
    self.collect()
    self.stage.db.close()
    self.stage = Staging(self.root / "volatile.db", BOOT)
    self.assertEqual(len(self.stage.records()), 1)
    self.stage.db.close()
    self.stage = Staging(self.root / "volatile.db", str(uuid.uuid4()))
    self.assertEqual(self.stage.records(), [])

  def test_parked_worker_restart_finalizes_last_short_checkpoint(self):
    self.collector.select_segment(("route", 0), 10 * NANO)
    self.collector.samples = {"fuel_percent": (10, 10 * NANO, "percent")}
    self.collector.checkpoint(11 * NANO)
    restarted = Collector(self.db, self.stage, trusted(), BOOT)
    restarted.select_segment(None, 12 * NANO)
    self.assertEqual(self.db.db.execute("SELECT count(*) FROM records").fetchone()[0], 1)

  def test_live_collection_does_not_require_a_logger_segment(self):
    self.collector.clock = trusted()
    self.collector.update_samples({"fuel_percent": (55, 10 * NANO, "percent")})
    envelope = self.collector.publish(12 * NANO, True)
    self.assertEqual(envelope["availability"], "current")
    self.assertEqual(envelope["snapshot"]["metrics"]["fuel_percent"]["value"], 55)
    self.assertEqual(self.db.db.execute("SELECT count(*) FROM records").fetchone()[0], 0)

  def test_measurement_time_frozen_across_anchor_refinement_and_worker_restart(self):
    self.collector.clock = trusted()
    self.collector.select_segment(("route", 0), 10 * NANO)
    sample = (55, 10 * NANO, "percent")
    self.collector.update_samples({"fuel_percent": sample})
    first = self.collector.publish(12 * NANO, True)["snapshot"]["metrics"]["fuel_percent"]["observed_at"]
    self.collector.clock.observe("gnss", 13 * NANO, UTC + 3 * NANO + 100_000_000, 13 * NANO)
    self.collector.update_samples({"fuel_percent": sample})  # Adapter repeats retained frames.
    second = self.collector.publish(14 * NANO, True)["snapshot"]["metrics"]["fuel_percent"]["observed_at"]
    self.assertEqual(first, second)
    self.collector.checkpoint(14 * NANO)
    new_clock = trusted()
    new_clock.anchor = ("gnss", 20 * NANO, UTC - 10 * NANO + 200_000_000)
    restarted = Collector(self.db, self.stage, new_clock, BOOT)
    restarted.select_vehicle("vin:test", "AUDI_A3_MK3", True)
    restarted.select_segment(("route", 0), 20 * NANO)
    self.assertEqual(restarted.publish(20 * NANO, True)["snapshot"]["metrics"]["fuel_percent"]["observed_at"], first)

  def test_corrected_unfinished_checkpoint_survives_power_loss_with_original_utc(self):
    self.collector.clock = trusted()
    self.collector.select_segment(("route", 0), 10 * NANO)
    self.collector.samples = {"fuel_percent": (10, 10 * NANO, "percent")}
    self.collector.checkpoint(11 * NANO)
    original = self.db.db.execute("SELECT payload FROM checkpoints").fetchone()[0]
    new_boot = str(uuid.uuid4())
    self.stage.db.close()
    self.stage = Staging(self.root / "volatile.db", new_boot)
    self.db.boot = new_boot
    restarted = Collector(self.db, self.stage, TrustedClock(), new_boot)
    restarted.select_segment(None, NANO)
    self.assertEqual(self.db.db.execute("SELECT payload FROM records").fetchone()[0], original)
    self.assertEqual(self.db.db.execute("SELECT count(*) FROM checkpoints").fetchone()[0], 0)

  def test_corrected_queue_survives_reboot_and_claim_recovers(self):
    payload = self.make_payload()
    _, rows = self.db.claim(NANO)
    self.assertEqual(len(rows), 1)
    self.db.close()
    self.db = Outbox(self.root / "outbox.db", str(uuid.uuid4()))
    _, recovered = self.db.claim(0)
    self.assertEqual(json.loads(recovered[0]["payload"]), payload)
    self.assertGreater(self.db.new_epoch(), payload["snapshot"]["collector_epoch"])

  def test_legacy_highwater_is_incremented_without_wall_time(self):
    self.assertEqual(self.db.meta("epoch"), 1_800_000_000_001)

  def test_record_is_immutable_and_crash_cleanup_uses_existing_digest(self):
    self.collect()
    record = self.stage.records()[0][0]
    self.collector.clock = trusted()
    self.collector.flush()
    self.stage.save(record, ready=True)
    self.collector.clock.anchor = ("gnss", 30 * NANO, UTC - 20 * NANO)
    self.collector.flush()
    self.assertEqual(self.db.db.execute("SELECT count(*) FROM records").fetchone()[0], 1)
    row = self.db.db.execute("SELECT * FROM records").fetchone()
    payload = json.loads(row["payload"])
    payload["route"] = "different"
    with self.assertRaises(ValueError):
      self.db.enqueue(row["id"], payload)

  def test_batch_limit_wire_digest_partial_ack_retry_quarantine(self):
    payload = self.make_payload()
    for _ in range(104):
      self.db.enqueue(str(uuid.uuid4()), payload)
    token, rows = self.db.claim(0)
    self.assertEqual(len(rows), 100)
    body = self.db.body(rows)
    self.assertLessEqual(len(body), BYTE_LIMIT)
    parsed = json.loads(body)
    self.assertEqual(len(parsed["records"]), 100)
    self.assertIn(rows[0]["payload"].encode(), body)
    results = [{"record_id": rows[0]["id"], "payload_sha256": rows[0]["digest"], "status": "accepted"},
               {"record_id": rows[1]["id"], "payload_sha256": "wrong", "status": "accepted"},
               {"record_id": rows[2]["id"], "payload_sha256": rows[2]["digest"], "status": "rejected", "error": "invalid"}]
    self.db.finish(token, rows, results, 0, 5)
    states = dict(self.db.db.execute("SELECT id,state FROM records"))
    self.assertEqual(states[rows[0]["id"]], "acknowledged")
    self.assertEqual(states[rows[1]["id"]], "pending")
    self.assertEqual(states[rows[2]["id"]], "quarantined")
    _, next_batch = self.db.claim(0)
    self.assertEqual(len(next_batch), 5)

  def test_lost_ack_is_retried_and_only_matching_duplicate_retires(self):
    self.make_payload()
    calls = []
    now = [0]

    def send(body):
      calls.append(body)
      if len(calls) == 1:
        raise TimeoutError
      records = json.loads(body)["records"]
      return 200, {"results": [{"record_id": row["record_id"], "payload_sha256": row["payload_sha256"], "status": "duplicate"}
                               for row in records]}, None

    uploader = Uploader(self.db, send, lambda: now[0], lambda: 1)
    self.assertFalse(uploader.step())
    now[0] = 5 * NANO
    self.assertTrue(uploader.step())
    self.assertEqual(calls[0], calls[1])
    self.assertFalse(uploader.step())

  def test_two_connections_cannot_claim_same_records(self):
    self.make_payload()
    self.db.claim(0)
    other = Outbox(self.root / "outbox.db", BOOT)
    try:
      self.assertEqual(other.claim(0)[1], [])
      self.assertEqual(len(other.claim(121 * NANO)[1]), 1)
    finally:
      other.close()

  def test_vehicle_change_hides_old_cache_before_clock_recovers(self):
    self.make_payload()
    self.collector.publish(12 * NANO, True)
    self.collector.clock.anchor = None
    self.collector.select_vehicle("trip:new", "UNKNOWN", False)
    self.assertIsNone(self.collector.publish(13 * NANO, True)["snapshot"])
    self.assertEqual(self.collector.publish(13 * NANO, True)["availability"], "unsupported")
    self.assertIsNone(self.db.meta("latest"))
    self.assertTrue(self.db.meta("invalidated"))

  def test_native_segment_locks_not_wall_minutes(self):
    for segment in (0, 3):
      path = self.root / f"route--{segment}"
      path.mkdir()
      (path / "rlog.lock").touch()
    self.assertEqual(current_segment(self.root, "route"), ("route", 3))
    self.assertIsNone(current_segment(self.root, "../escape"))
    self.assertIsNone(current_segment(self.root, "other"))


if __name__ == "__main__":
  unittest.main()
