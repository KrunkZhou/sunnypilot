import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openpilot.system.vehicle_telemetry.clock import NANO
from openpilot.system.vehicle_telemetry.collector import Collector
from openpilot.system.vehicle_telemetry.state import MAX_ORDER
from openpilot.system.vehicle_telemetry.store import Outbox, Staging
from openpilot.system.vehicle_telemetry.tests.test_queue import BOOT, trusted
from openpilot.system.vehicle_telemetry.uploader import OrderingSync, Uploader, read_ordering, upload_loop

SERVER_EPOCH = 1_785_251_099_704


class TestOrderingTransport(unittest.TestCase):
  def probe(self, chunks, status=200, dongle="test-device"):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = status
    response.iter_content.return_value = chunks
    requests = MagicMock()
    requests.get.return_value = response
    api = SimpleNamespace(service=SimpleNamespace(api_host="https://api.example.test"), get_token=lambda: "test-jwt")
    modules = {"requests": requests, "openpilot.common.api": SimpleNamespace(Api=lambda _: api),
               "openpilot.common.params": SimpleNamespace(Params=lambda: SimpleNamespace(get=lambda _: dongle))}
    with patch.dict("sys.modules", modules):
      result = read_ordering()
    return result, requests, response

  def test_primary_authenticated_get_is_bounded_and_does_not_redirect(self):
    result, requests, response = self.probe([b'{"collector_epoch":1785251099704,', b'"snapshot_seq":256}'])
    self.assertEqual(result, (200, {"collector_epoch": SERVER_EPOCH, "snapshot_seq": 256}))
    requests.get.assert_called_once_with("https://api.example.test/v1/devices/test-device/vehicle-telemetry/ordering",
                                        headers={"Authorization": "JWT test-jwt", "Cache-Control": "no-cache"},
                                        timeout=(5, 5), stream=True, allow_redirects=False)
    response.iter_content.assert_called_once_with(1024)
    response.__exit__.assert_called_once()

  def test_bad_json_and_excessive_response_are_rejected(self):
    for chunks in [[b"not json"], [b" " * 4096, b"{}"]]:
      with self.assertRaises(ValueError):
        self.probe(chunks)

  def test_error_response_body_is_not_read_and_unregistered_never_calls(self):
    result, _, response = self.probe([b" " * 100000], status=404)
    self.assertEqual(result, (404, {}))
    response.iter_content.assert_not_called()
    for dongle in [None, "UnregisteredDevice"]:
      result, requests, _ = self.probe([], dongle=dongle)
      self.assertEqual(result, (401, {}))
      requests.get.assert_not_called()


class TestOrderingRecovery(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.path = Path(self.temp.name) / "outbox.sqlite3"
    self.store = Outbox(self.path, BOOT)
    self.store.set_meta("epoch", 33)
    self.store.set_meta("seq", 256)

  def tearDown(self):
    self.store.close()
    self.temp.cleanup()

  def test_live_reset_recovers_future_order_without_mutating_existing_payloads(self):
    stage = Staging(Path(self.temp.name) / "staging.sqlite3", BOOT)
    collector = Collector(self.store, stage, trusted(), BOOT)
    try:
      collector.select_vehicle("trip:current", "AUDI_A3_MK3", True)
      collector.select_segment(("route", 0), 10 * NANO)
      collector.samples = {"fuel_percent": (63, 10 * NANO, "Kombi_03"), "odometer_km": (50290, 10 * NANO, "Kombi_02")}
      collector.checkpoint(11 * NANO)
      collector.select_segment(("route", 1), 12 * NANO)
      before_records = [tuple(row) for row in self.store.db.execute("SELECT * FROM records")]
      before_checkpoints = [tuple(row) for row in self.store.db.execute("SELECT * FROM checkpoints")]
      before_stage = stage.records()
      before_latest = self.store.meta("latest")
      # The uploader owns a separate connection while collection keeps running.
      other = Outbox(self.path, BOOT)
      try:
        self.assertTrue(other.reconcile_order(SERVER_EPOCH, 256))
      finally:
        other.close()
      self.assertEqual([tuple(row) for row in self.store.db.execute("SELECT * FROM records")], before_records)
      self.assertEqual([tuple(row) for row in self.store.db.execute("SELECT * FROM checkpoints")], before_checkpoints)
      self.assertEqual(stage.records(), before_stage)
      self.assertEqual(self.store.meta("latest"), before_latest)
      snapshot = collector.publish(13 * NANO, True)["snapshot"]
      self.assertEqual((snapshot["collector_epoch"], snapshot["snapshot_seq"]), (SERVER_EPOCH + 1, 1))
      self.assertEqual(snapshot["metrics"], before_latest["metrics"])
      collector.checkpoint(14 * NANO)
      collector.select_segment(None, 15 * NANO)
      payloads = [json.loads(row[0])["snapshot"] for row in self.store.db.execute("SELECT payload FROM records ORDER BY rowid")]
      self.assertEqual(payloads[0]["collector_epoch"], 34)
      self.assertEqual(payloads[1]["collector_epoch"], SERVER_EPOCH + 1)
      self.assertEqual(payloads[0]["metrics"], payloads[1]["metrics"])
    finally:
      stage.db.close()

  def test_recovery_survives_restart_and_never_rewinds(self):
    self.assertTrue(self.store.reconcile_order(SERVER_EPOCH, 256))
    self.store.close()
    self.store = Outbox(self.path, BOOT)
    self.assertEqual(self.store.next_order(), (SERVER_EPOCH + 1, 1))
    for epoch, seq in [(0, 0), (33, 9999), (SERVER_EPOCH + 1, 1)]:
      self.assertFalse(self.store.reconcile_order(epoch, seq))
    self.assertTrue(self.store.reconcile_order(SERVER_EPOCH + 1, 2))
    self.assertEqual(self.store.next_order(), (SERVER_EPOCH + 2, 1))

  def test_invalid_and_exhausted_watermarks_leave_metadata_unchanged(self):
    for epoch, seq in [(True, 1), (1, False), (1.0, 1), (1, 1.0), (None, 1), ("1", 1),
                       (-1, 1), (1, -1), (0, 1), (1, 0), (MAX_ORDER + 1, 1), (1, MAX_ORDER + 1), (MAX_ORDER, 1)]:
      with self.subTest(epoch=epoch, seq=seq), self.assertRaises(ValueError):
        self.store.reconcile_order(epoch, seq)
      self.assertEqual((self.store.meta("epoch"), self.store.meta("seq")), (33, 256))

  def test_reconcile_serializes_with_concurrent_collector_allocations(self):
    ready = threading.Barrier(2)

    def collect():
      store = Outbox(self.path, BOOT)
      try:
        ready.wait()
        return [store.next_order() for _ in range(100)]
      finally:
        store.close()

    def reconcile():
      store = Outbox(self.path, BOOT)
      try:
        ready.wait()
        return store.reconcile_order(SERVER_EPOCH, 256)
      finally:
        store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
      values, recovery = pool.submit(collect), pool.submit(reconcile)
      orders = values.result()
      self.assertTrue(recovery.result())
    self.assertEqual(orders, sorted(set(orders)))
    self.assertGreater(self.store.next_order(), max((SERVER_EPOCH, 256), *orders))

  def test_probe_runs_without_queued_records_and_rechecks_periodically(self):
    now, calls = [0], []

    def read():
      calls.append(now[0])
      return 200, {"collector_epoch": SERVER_EPOCH, "snapshot_seq": 256}

    sync = OrderingSync(self.store, read, lambda: now[0], lambda: 1)
    self.assertTrue(sync.step())
    self.assertFalse(sync.step())
    now[0] = 299 * NANO
    self.assertFalse(sync.step())
    now[0] += NANO
    self.assertFalse(sync.step())
    self.assertEqual(calls, [0, 300 * NANO])
    sync.reconnect()
    sync.step()
    self.assertEqual(len(calls), 3)

  def test_failed_probes_back_off_and_reconnect_retries(self):
    now, calls = [0], []

    def offline():
      calls.append(now[0])
      raise OSError("offline")

    sync = OrderingSync(self.store, offline, lambda: now[0], lambda: 1)
    sync.step()
    self.assertEqual(sync.next_attempt, 5 * NANO)
    sync.step()
    self.assertEqual(len(calls), 1)
    now[0] = 5 * NANO
    sync.step()
    self.assertEqual(sync.next_attempt, 15 * NANO)
    sync.reconnect()
    sync.step()
    self.assertEqual(len(calls), 3)
    for _ in range(20):
      now[0] = sync.next_attempt
      sync.step()
    self.assertEqual(sync.next_attempt - now[0], 3600 * NANO)

  def test_unsupported_server_retries_hourly_even_on_reconnect(self):
    for status in [404, 405, 410]:
      sync = OrderingSync(self.store, lambda status=status: (status, {}), lambda: 0, lambda: 1)
      sync.step()
      sync.reconnect()
      self.assertEqual(sync.next_attempt, 3600 * NANO)
      self.assertEqual((self.store.meta("epoch"), self.store.meta("seq")), (33, 256))

  def test_recovered_clock_or_connection_wakes_authentication_retry(self):
    for status in [401, 403]:
      sync = OrderingSync(self.store, lambda status=status: (status, {}), lambda: 0, lambda: 1)
      sync.step()
      self.assertEqual(sync.next_attempt, 3600 * NANO)
      sync.reconnect()
      sync.read = lambda: (200, {"collector_epoch": SERVER_EPOCH, "snapshot_seq": 256})
      sync.step()
      self.assertEqual(self.store.meta("epoch"), SERVER_EPOCH + 1)
      self.assertEqual(sync.next_attempt, 300 * NANO)

  def test_malformed_probes_do_not_change_ordering(self):
    for value in [None, [], {}, {"collector_epoch": SERVER_EPOCH}, {"collector_epoch": True, "snapshot_seq": 1}]:
      sync = OrderingSync(self.store, lambda value=value: (200, value), lambda: 0, lambda: 1)
      self.assertFalse(sync.step())
      self.assertEqual(sync.next_attempt, 5 * NANO)
      self.assertEqual((self.store.meta("epoch"), self.store.meta("seq")), (33, 256))

  def test_worker_probes_before_empty_queue_and_failure_does_not_block_delivery(self):
    # Exercise the actual worker orchestration with no native Athena runtime.
    for status in [200, 404]:
      stopped = threading.Event()
      actions = []

      def probe(actions=actions, status=status):
        actions.append("probe")
        return status, {"collector_epoch": SERVER_EPOCH, "snapshot_seq": 256}

      def batch_step(actions=actions, stopped=stopped):
        actions.append("batch")
        stopped.set()
        return False

      sync = OrderingSync(self.store, probe, lambda: 0, lambda: 1)
      with patch("openpilot.system.vehicle_telemetry.uploader.OrderingSync", return_value=sync), \
           patch.object(Uploader, "step", side_effect=batch_step), \
           patch.dict("sys.modules", {"openpilot.common.swaglog": type("Log", (), {"cloudlog": None})}):
        upload_loop(self.path, BOOT, stopped)
      self.assertEqual(actions, ["probe", "batch"])


if __name__ == "__main__":
  unittest.main()
