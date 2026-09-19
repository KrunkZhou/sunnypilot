"""Portable tests for native Params submission and conservative map observation."""
import ast
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpilot.sunnypilot.system import remote_maps as maps
from openpilot.sunnypilot.system import remote_workflows as workflows
from openpilot.sunnypilot.system.remote_settings import DeviceState
from openpilot.sunnypilot.system.remote_workflows import RequestState


class Params:
  def __init__(self, values=None):
    self.values = dict(values or {})
    self.writes = []

  def get(self, key, return_default=False):
    return self.values.get(key, "All" if return_default and key == "OsmStateName" else None)

  def get_bool(self, key):
    return bool(self.get(key))

  def put(self, key, value, block=False):
    self.writes.append((key, value))
    self.values[key] = value

  put_bool = put

  def remove(self, key):
    self.writes.append((key, None))
    self.values.pop(key, None)


class TestRemoteMaps(unittest.TestCase):
  def setUp(self):
    self.store = RequestState()
    self.params = Params({"MapdVersion": "v1.12.0", "OsmLocationName": "CA", "IsOffroad": True})
    self.mem = Params()
    self.live = DeviceState(False, False)
    self.service = maps.RemoteMaps(self.params, self.mem, self.store, state=lambda: self.live)

  def command(self, action="download", **kwargs):
    snapshot = self.service.snapshot()
    args = {"expected_selection": snapshot["selection"], "expected_operation_id": (snapshot["operation"] or {}).get("id")}
    if action == "download":
      args.update(country="US", state="CA", country_title="United States", state_title="California")
    if action == "delete":
      args["confirmed"] = True
    args.update(kwargs)
    return self.service.manage(action, **args)

  def start(self):
    self.params.put_bool("OsmDbUpdatesCheck", False)
    self.mem.put("OSMDownloadLocations", maps._targets(maps._selection(self.params)))
    return self.service.snapshot()

  def progress(self, downloaded, total, locations=None):
    self.params.put("OSMDownloadProgress", {"downloaded_files": downloaded, "total_files": total,
                                           "locations_to_download": locations or ["CA"]})

  def test_snapshot_never_writes_params_or_fetches_catalog(self):
    with patch("urllib.request.urlopen", side_effect=AssertionError("network read")):
      snapshot = self.service.snapshot()
    self.assertTrue(snapshot["available"])
    self.assertEqual(snapshot["selection"], {"country": "CA", "state": None})
    self.assertEqual(self.params.writes, [])
    self.assertEqual(self.mem.writes, [])
    self.assertIsNone(snapshot["cache_bytes"])
    self.assertNotIn("catalog", snapshot)

  def test_arbitrary_or_missing_mapd_version_does_not_block_requests(self):
    for version in (None, "another-version", 1):
      for action in ("download", "check_updates", "delete"):
        with self.subTest(version=version, action=action):
          self.params.values = {"MapdVersion": version, "OsmLocationName": "CA", "IsOffroad": True}
          self.store.write({})
          snapshot = self.service.snapshot()
          self.assertTrue(snapshot["available"])
          self.assertEqual(snapshot["mapd_version"], version if isinstance(version, str) else "")
          self.assertTrue(all(row["allowed"] for row in snapshot["actions"].values()))
          self.assertEqual(self.command(action)["status"], "accepted")
          self.assertEqual(self.service.snapshot()["operation"]["status"], "pending")

  def test_version_change_marks_active_receipt_unknown_without_blocking_api(self):
    self.params.values["MapdVersion"] = "custom-before"
    self.command()
    self.start()
    self.progress(2, 2)
    self.mem.remove("OSMDownloadLocations")
    self.params.values["MapdVersion"] = "custom-after"
    snapshot = self.service.snapshot()
    self.assertTrue(snapshot["available"])
    self.assertEqual(snapshot["operation"]["status"], "unknown")
    self.assertTrue(snapshot["actions"]["delete"]["allowed"])

  def test_commands_reject_unknown_fields_and_wrong_types(self):
    for kwargs in ({"extra": True}, {"country": []}, {"state": True}, {"expected_operation_id": 12},
                   {"expected_selection": {"country": "CA"}}):
      with self.subTest(kwargs=kwargs):
        self.assertEqual(self.command(**kwargs)["status"], "rejected")
    self.assertEqual(self.service.manage("download")["status"], "rejected")
    self.assertEqual(self.service.manage([])["status"], "rejected")
    self.assertEqual(self.command("delete", confirmed=1)["status"], "rejected")
    self.assertEqual(self.params.writes, [])

  def test_region_validation(self):
    for country, state in (("X", None), ("usa", None), ("ca", None), ("CÉ", None), ("CA", "ON"),
                           ("US", "California"), ("US", "ca"), ("US", "ÄZ"), ("US", "ALL"), ("US", None)):
      self.assertEqual(self.command(country=country, state=state)["status"], "rejected")
    self.assertEqual(self.params.writes, [])

  def test_catalog_membership_is_delegated_to_rtzs(self):
    self.assertEqual(self.command(country="XX", state=None, country_title="Server country", state_title=None)["status"], "accepted")
    self.params.remove("OsmDbUpdatesCheck")
    self.assertEqual(self.command(state="ZZ", state_title="Server state")["status"], "accepted")
    self.assertEqual(self.params.get("OsmStateTitle"), "Server state")

  def test_server_labels_are_required_bounded_and_written_verbatim(self):
    for kwargs in ({"country_title": None}, {"country_title": ""}, {"country_title": "x" * 257}, {"country_title": True},
                   {"state_title": None}, {"state_title": ""}, {"state_title": "x" * 257}, {"state_title": []},
                   {"country": "CA", "state": None, "state_title": "unexpected"}):
      with self.subTest(kwargs=kwargs):
        self.assertEqual(self.command(**kwargs)["status"], "rejected")
    self.assertEqual(self.params.writes, [])
    args = {"country": "US", "state": "CA", "expected_selection": {"country": "CA", "state": None}, "expected_operation_id": None}
    self.assertEqual(self.service.manage("download", **args)["status"], "rejected")
    country_title, state_title = "Country from server", "State from server"
    self.assertEqual(self.command(country_title=country_title, state_title=state_title)["status"], "accepted")
    self.assertEqual(self.params.get("OsmLocationTitle"), country_title)
    self.assertEqual(self.params.get("OsmStateTitle"), state_title)

  def test_download_writes_native_selection_then_trigger_only(self):
    self.progress(10, 10)
    self.params.writes.clear()
    result = self.command()
    self.assertEqual(result["status"], "accepted")
    self.assertEqual(result["snapshot"]["operation"]["status"], "pending")
    self.assertEqual(result["snapshot"]["selection"], {"country": "US", "state": "CA"})
    self.assertEqual(self.params.writes, [("OsmLocal", True), ("OsmStateName", None), ("OsmStateTitle", None),
                                        ("OsmLocationName", "US"), ("OsmLocationTitle", "United States"),
                                        ("OsmStateName", "CA"), ("OsmStateTitle", "California"), ("OsmDbUpdatesCheck", True)])
    self.assertEqual(self.mem.writes, [])
    self.assertEqual(self.params.get("OSMDownloadProgress")["downloaded_files"], 10)
    self.assertEqual(self.command()["status"], "rejected")

  def test_country_change_clears_old_state_and_all_is_supported(self):
    self.assertEqual(self.command(state="All", state_title="All states")["status"], "accepted")
    self.params.put_bool("OsmDbUpdatesCheck", False)
    self.assertEqual(self.command(country="CA", state=None, country_title="Canada", state_title=None)["status"], "accepted")
    self.assertIsNone(self.params.get("OsmStateName"))
    self.assertIsNone(self.params.get("OsmStateTitle"))

  def test_check_updates_and_delete_write_only_existing_flag(self):
    result = self.command("check_updates")
    self.assertEqual(result["status"], "accepted")
    self.assertEqual(self.params.writes, [("OsmDbUpdatesCheck", True)])
    self.assertEqual(result["snapshot"]["selection"], {"country": "CA", "state": None})
    self.params.put_bool("OsmDbUpdatesCheck", False)
    self.params.writes.clear()
    self.assertEqual(self.command("delete")["status"], "accepted")
    self.assertEqual(self.params.writes, [("Mapd_ClearCache", True)])
    self.assertEqual(self.mem.writes, [])

  def test_selection_and_receipt_preconditions_prevent_uncertain_retry(self):
    self.assertEqual(self.command(expected_selection={"country": "US", "state": "CA"})["status"], "conflict")
    self.assertEqual(self.command()["status"], "accepted")
    before = list(self.params.writes)
    self.assertEqual(self.command(expected_operation_id=None)["status"], "conflict")
    self.assertEqual(self.params.writes, before)

  def test_legacy_selection_can_be_deleted_or_replaced(self):
    self.params.values["OsmLocationName"] = "legacy-region"
    self.assertEqual(self.command("delete")["status"], "accepted")
    self.params.remove("Mapd_ClearCache")
    self.assertEqual(self.command()["status"], "accepted")
    self.assertEqual(maps._selection(self.params), {"country": "US", "state": "CA"})

  def test_fresh_offroad_admission_fails_closed(self):
    for live, offroad in ((DeviceState(True, False), True), (DeviceState(), True), (DeviceState(False, False), False),
                          (DeviceState(False, False), 1)):
      self.live = live
      self.params.values["IsOffroad"] = offroad
      self.assertEqual(self.command()["status"], "rejected")
    self.assertEqual(self.params.writes, [])

  def test_admission_rechecks_live_state_after_snapshot(self):
    self.assertTrue(self.service.snapshot()["actions"]["download"]["allowed"])
    self.live = DeviceState(True, False)
    self.assertEqual(self.service.manage("download", country="US", state="CA", country_title="United States", state_title="California",
                                        expected_selection={"country": "CA", "state": None},
                                        expected_operation_id=None)["status"], "rejected")
    self.assertEqual(self.params.writes, [])

  def test_native_flags_and_even_malformed_markers_block_remote(self):
    for target, key, value in ((self.params, "OsmDbUpdatesCheck", True), (self.params, "Mapd_ClearCache", True),
                               (self.mem, "OSMDownloadLocations", "bad json"), (self.mem, "OSMDownloadBounds", "busy")):
      target.values[key] = value
      self.assertEqual(self.command()["status"], "rejected")
      target.values.pop(key)
    self.assertEqual(self.params.writes, [])

  def test_marker_never_observed_is_unknown_even_with_complete_progress(self):
    self.command()
    self.params.put_bool("OsmDbUpdatesCheck", False)
    self.progress(2, 2)
    operation = self.service.snapshot()["operation"]
    self.assertEqual(operation["status"], "unknown")
    self.assertNotIn("progress", operation)

  def test_stale_matching_progress_cannot_prove_success(self):
    self.progress(2, 2)
    self.command()
    self.assertEqual(self.start()["operation"]["status"], "running")
    self.assertNotIn("progress", self.service.snapshot()["operation"])
    self.mem.remove("OSMDownloadLocations")
    self.assertEqual(self.service.snapshot()["operation"]["status"], "unknown")

  def test_matching_fresh_progress_can_report_completed_or_incomplete(self):
    for done, expected in ((2, "completed"), (1, "incomplete")):
      with self.subTest(expected=expected):
        self.params.values.pop("OSMDownloadProgress", None)
        self.command()
        self.start()
        self.progress(done, 2)
        self.assertEqual(self.service.snapshot()["operation"]["progress"], {"downloaded": done, "total": 2})
        self.mem.remove("OSMDownloadLocations")
        self.assertEqual(self.service.snapshot()["operation"]["status"], expected)

  def test_observed_new_progress_before_identical_old_final_can_complete(self):
    self.progress(2, 2)
    self.command()
    self.start()
    self.progress(1, 2)
    self.service.snapshot()
    self.progress(2, 2)
    self.mem.remove("OSMDownloadLocations")
    self.assertEqual(self.service.snapshot()["operation"]["status"], "completed")

  def test_missing_malformed_or_unrelated_final_progress_is_unknown(self):
    for value in (None, "bad json", {"downloaded_files": True, "total_files": 2, "locations_to_download": ["CA"]},
                  json.dumps({"downloaded_files": 2, "total_files": 2, "locations_to_download": ["CA"]}),
                  {"downloaded_files": 2, "total_files": 2, "locations_to_download": ["MX"]}):
      with self.subTest(value=value):
        self.command()
        self.start()
        self.params.put("OSMDownloadProgress", value)
        self.mem.remove("OSMDownloadLocations")
        self.assertEqual(self.service.snapshot()["operation"]["status"], "unknown")

  def test_changed_selection_or_marker_makes_receipt_permanently_unknown(self):
    self.command()
    self.start()
    self.mem.put("OSMDownloadLocations", {"nations": ["MX"], "states": []})
    self.assertEqual(self.service.snapshot()["operation"]["status"], "unknown")
    self.mem.remove("OSMDownloadLocations")
    self.progress(2, 2)
    self.assertEqual(self.service.snapshot()["operation"]["status"], "unknown")

  def test_athena_restart_cannot_replay_or_correlate_old_progress(self):
    self.command()
    self.start()
    self.progress(2, 2)
    self.params.writes.clear()
    self.mem.writes.clear()
    restarted = maps.RemoteMaps(self.params, self.mem, state=lambda: self.live)
    self.assertEqual(restarted.snapshot()["operation"]["status"], "unknown")
    self.mem.values.pop("OSMDownloadLocations")
    self.assertEqual(restarted.snapshot()["operation"]["status"], "unknown")
    self.assertEqual(self.params.writes, [])
    self.assertEqual(self.mem.writes, [])

  def test_new_native_activity_replaces_completed_receipt_without_claiming_success(self):
    self.command()
    self.start()
    self.progress(2, 2)
    self.mem.remove("OSMDownloadLocations")
    completed = self.service.snapshot()["operation"]
    self.assertEqual(completed["status"], "completed")
    self.mem.put("OSMDownloadLocations", {"nations": [], "states": ["CA"]})
    native = self.service.snapshot()["operation"]
    self.assertEqual(native["status"], "unknown")
    self.assertNotEqual(native["id"], completed["id"])
    self.assertEqual(self.service.snapshot()["operation"]["id"], native["id"])
    self.mem.remove("OSMDownloadLocations")
    self.assertEqual(self.service.snapshot()["operation"]["status"], "unknown")
    self.assertEqual(self.service.snapshot()["operation"]["id"], native["id"])

  def test_new_native_request_after_consumed_unknown_invalidates_old_precondition(self):
    self.command("check_updates")
    self.params.remove("OsmDbUpdatesCheck")
    previous = self.service.snapshot()["operation"]
    self.assertEqual(previous["status"], "unknown")
    self.params.put_bool("OsmDbUpdatesCheck", True)
    result = self.service.manage("delete", confirmed=True, expected_selection={"country": "CA", "state": None},
                                 expected_operation_id=previous["id"])
    self.assertEqual(result["status"], "conflict")
    native = result["snapshot"]["operation"]
    self.assertNotEqual(native["id"], previous["id"])
    self.assertEqual(native["status"], "unknown")
    self.params.remove("OsmDbUpdatesCheck")
    self.mem.put("OSMDownloadLocations", {"nations": ["CA"], "states": []})
    self.assertEqual(self.service.snapshot()["operation"]["id"], native["id"])

  def test_delete_never_claims_completion_from_flag_or_metadata(self):
    self.command("delete")
    self.params.remove("Mapd_ClearCache")
    for key in ("OsmLocationName", "OsmLocal", "OsmDownloadedDate"):
      self.params.remove(key)
    self.service.size_reader = lambda: 0
    self.assertEqual(self.service.snapshot()["operation"]["status"], "unknown")

  def test_partial_submission_failure_does_not_rollback_or_retry(self):
    original = self.params.put_bool
    def failing_put(key, value, block=False):
      if key == "OsmDbUpdatesCheck":
        raise OSError("failed")
      original(key, value, block)
    with patch.object(self.params, "put_bool", failing_put):
      result = self.command()
    self.assertEqual(result["status"], "failed")
    self.assertEqual(result["snapshot"]["operation"]["status"], "unknown")
    self.assertEqual(maps._selection(self.params), {"country": "US", "state": "CA"})
    before = list(self.params.writes)
    self.service.snapshot()
    self.assertEqual(self.params.writes, before)

  def test_remote_calls_serialize_without_a_worker_queue(self):
    entered, release = threading.Event(), threading.Event()
    initial_thread = threading.current_thread()
    def live():
      if threading.current_thread() is not initial_thread:
        entered.set()
        release.wait(2)
      return self.live
    self.service.state = live
    first = []
    args = {"country": "US", "state": "CA", "country_title": "United States", "state_title": "California",
            "expected_selection": {"country": "CA", "state": None}, "expected_operation_id": None}
    thread = threading.Thread(target=lambda: first.append(self.service.manage("download", **args)))
    thread.start()
    try:
      self.assertTrue(entered.wait(1))
      second = self.service.manage("download", **args)
      self.assertEqual(second["status"], "rejected")
    finally:
      release.set()
      thread.join(2)
    self.assertEqual(first[0]["status"], "accepted")
    self.assertEqual(sum(key == "OsmDbUpdatesCheck" for key, _ in self.params.writes), 1)

  def test_invalid_last_checked_is_not_displayed(self):
    for value in ("nan", "inf", "bad", "-1"):
      self.params.values["OsmDownloadedDate"] = value
      self.assertIsNone(self.service.snapshot()["last_checked"])

  def test_existing_manager_consumes_adapter_trigger_without_hooks(self):
    source = Path(maps.__file__).parents[1] / "mapd" / "mapd_manager.py"
    names = {"request_refresh_osm_location_data", "filter_nations_and_states", "update_osm_db"}
    tree = ast.parse(source.read_text())
    selected = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[])
    env = {"params": self.params, "mem_params": self.mem, "datetime": datetime, "json": json,
           "cleanup_old_osm_data": Mock(), "get_files_for_cleanup": list, "cloudlog": SimpleNamespace(info=lambda *args: None)}
    exec(compile(selected, str(source), "exec"), env)
    self.command()
    env["update_osm_db"]()
    self.assertFalse(self.params.get("OsmDbUpdatesCheck"))
    self.assertEqual(self.mem.get("OSMDownloadLocations"), {"nations": [], "states": ["CA"]})
    self.assertEqual(self.service.snapshot()["operation"]["status"], "running")


class TestMapSizeCache(unittest.TestCase):
  def test_size_scan_is_bounded_and_skips_symlinks(self):
    with tempfile.TemporaryDirectory() as root:
      path = Path(root)
      (path / "one").write_bytes(b"abc")
      (path / "link").symlink_to(path / "one")
      cache = workflows.BackgroundSizeCache(path)
      with patch.object(workflows.time, "monotonic", side_effect=[0, 2, 3]):
        cache._scan()
      self.assertIsNone(cache.value)
      cache._scan()
      self.assertEqual(cache.value, 3)


if __name__ == "__main__":
  unittest.main()
