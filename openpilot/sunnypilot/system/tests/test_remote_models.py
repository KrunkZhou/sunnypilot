"""Portable tests for the thin native-Params model adapter."""
import json
import math
import time
import unittest
from unittest.mock import patch

from openpilot.sunnypilot.system.remote_models import RemoteModels, SLOTS, SYNC_KEYS, _catalog
from openpilot.sunnypilot.system.remote_workflows import RequestState, WorkflowBusy


class FakeParams:
  def __init__(self):
    self.values = {"IsOffroad": True}
    self.writes = []
    self.after_write = None

  def get(self, key, **_):
    return self.values.get(key)

  def put(self, key, value, block=False):
    self.values[key] = value
    self.writes.append((key, value))
    if self.after_write:
      self.after_write(key, value)

  def remove(self, key):
    self.values.pop(key, None)
    self.writes.append((key, None))
    if self.after_write:
      self.after_write(key, None)


class TestRemoteModels(unittest.TestCase):
  def setUp(self):
    self.params = FakeParams()
    self.requests = RequestState()
    self.live = {"started": False, "chestnut_present": False, "selected": {}}
    self.catalog = [{"ref": ref, "source": source, "name": name, "short_name": ref, "folder": "Testing"}
                    for source, ref, name in (("qcom", "small", "Small model"), ("chestnut", "big", "Big model"))]
    self.remote = RemoteModels(self.params, self.requests, state=lambda: self.live, catalog=lambda: self.catalog,
                               defaults={"qcom": "Small default", "chestnut": "Big default"})

  def select(self, **kwargs):
    return self.remote.manage("select", **{"source": "qcom", "ref": "small", "expected_ref": None, "expected_operation_id": None, **kwargs})

  def test_snapshot_contains_both_hardware_slots_and_no_download_urls(self):
    snapshot = self.remote.snapshot()
    self.assertTrue(snapshot["available"])
    self.assertEqual([slot["source"] for slot in snapshot["slots"]], ["qcom", "chestnut"])
    self.assertEqual(snapshot["hardware"], {"chestnut_present": False})
    self.assertEqual(snapshot["bundles"][0]["folder"], "Testing")
    self.assertIsNone(snapshot["cache_bytes"])
    self.assertTrue(snapshot["slots"][0]["available"])
    self.assertFalse(snapshot["slots"][1]["available"])
    self.assertEqual(self.params.writes, [])

  def test_snapshot_only_reads_cached_size(self):
    self.remote.cache_size = lambda: 1234
    with patch("os.scandir", side_effect=AssertionError("RPC must not scan")):
      self.assertEqual(self.remote.snapshot()["cache_bytes"], 1234)

  def test_catalog_maps_only_compatible_metadata_without_parsing_artifacts(self):
    raw = {"ref": "safe", "display_name": "Safe", "short_name": "safe", "index": 5, "minimum_selector_version": 19,
           "build_time": "2026-09-01T06:52:40Z", "description": "Catalog-authored model notes.",
           "models": [{"artifact": {"file_name": "../forbidden", "chunks": [{"file_name": "unsafe"}]}}],
           "overrides": {"folder": "Testing"}}
    self.params.values["ModelManager_ModelsCache"] = {"bundles": [raw, {**raw, "ref": "future", "minimum_selector_version": 20}]}
    with patch("builtins.open", side_effect=AssertionError("Catalog must not write manifests")):
      catalog = _catalog(self.params, compatible=lambda bundle: bundle["minimumSelectorVersion"] == 19)
    self.assertEqual([bundle["ref"] for bundle in catalog], ["safe"])
    self.assertNotIn("models", catalog[0])
    self.remote.catalog = lambda: catalog
    bundle = self.remote.snapshot()["bundles"][0]
    self.assertEqual(bundle["build_date"], "2026-09-01T06:52:40Z")
    self.assertEqual(bundle["description"], "Catalog-authored model notes.")
    self.assertEqual(self.params.writes, [])

  def test_absent_catalog_notes_are_not_invented(self):
    bundle = self.remote.snapshot()["bundles"][0]
    self.assertNotIn("description", bundle)
    self.assertNotIn("build_date", bundle)

  def test_catalog_preserves_source_order_latest_duplicate_and_metadata_limits(self):
    def bundle(ref, index, **kwargs):
      return {"ref": ref, "index": index, "minimum_selector_version": 19, **kwargs}

    self.params.values["ModelManager_ModelsCache"] = {"bundles": [
      bundle("shared", 1, display_name="Older"), bundle("first-tie", 9),
      bundle("shared", 10, display_name="Newest", short_name="new", overrides={"folder": "Testing", "ignored": "value"},
             description="d" * 8192, build_time="b" * 8193), bundle("second-tie", 9),
      bundle("bad;ref", 99), bundle("wrong-version", 99, minimum_selector_version=20),
      bundle("bad-index", "invalid"), None,
    ]}
    self.params.values["ModelManager_ModelsCache_Chestnut"] = {"bundles": [bundle("shared", 100, overrides=[])]}
    catalog = _catalog(self.params, compatible=lambda value: value["minimumSelectorVersion"] == 19)
    self.assertEqual([(item["source"], item["ref"]) for item in catalog],
                     [("qcom", "shared"), ("qcom", "first-tie"), ("qcom", "second-tie"), ("chestnut", "shared")])
    self.assertEqual(catalog[0], {"ref": "shared", "source": "qcom", "name": "Newest", "short_name": "new", "folder": "Testing",
                                "description": "d" * 8192})
    self.assertEqual(catalog[-1]["folder"], "")
    self.assertEqual(self.params.writes, [])

  def test_catalog_enforces_one_global_bundle_limit_after_deduplication(self):
    for qcom_count in (4095, 4096, 4097):
      with self.subTest(qcom_count=qcom_count):
        bundles = [{"ref": f"model-{index}", "index": index, "minimum_selector_version": 19} for index in range(qcom_count)]
        self.params.values["ModelManager_ModelsCache"] = {"bundles": [*bundles, bundles[-1]]}
        self.params.values["ModelManager_ModelsCache_Chestnut"] = {"bundles": [{"ref": "big", "minimum_selector_version": 19}]}
        catalog = _catalog(self.params, compatible=lambda value: value["minimumSelectorVersion"] == 19)
        self.assertEqual(len(catalog), 4096)
        self.assertEqual(catalog[0]["ref"], f"model-{qcom_count - 1}")
        self.assertEqual(catalog[-1]["source"], "chestnut" if qcom_count == 4095 else "qcom")

  def test_catalog_failure_preserves_selected_slots(self):
    self.remote.catalog = lambda: 1 / 0
    self.params.values[SLOTS["qcom"]] = {"ref": "selected", "displayName": "Selected"}
    snapshot = self.remote.snapshot()
    self.assertEqual(snapshot["bundles"], [])
    self.assertEqual(snapshot["slots"][0]["selected_ref"], "selected")
    self.assertFalse(snapshot["actions"]["select"]["allowed"])
    self.assertTrue(snapshot["actions"]["reset"]["allowed"])

  def test_select_immediately_writes_only_native_trigger(self):
    result = self.select()
    self.assertEqual(result["status"], "accepted")
    self.assertEqual(result["snapshot"]["operation"]["status"], "pending")
    self.assertEqual(self.params.writes, [("ModelManager_DownloadRef", "small")])
    self.assertIsNone(self.params.get(SLOTS["qcom"]))

  def test_stale_or_onroad_state_blocks_request(self):
    for started in (None, True):
      self.live["started"] = started
      self.assertEqual(self.select()["status"], "rejected")
    self.live["started"] = False
    self.params.values["IsOffroad"] = False
    self.assertEqual(self.select()["status"], "rejected")
    self.assertEqual(self.params.writes, [])

  def test_state_is_read_after_acquiring_submission_lock(self):
    def live():
      self.assertTrue(self.requests._lock._is_owned())
      return self.live
    self.remote.state = live
    self.assertEqual(self.select()["status"], "accepted")

  def test_changed_selection_and_operation_conflict(self):
    self.params.values[SLOTS["qcom"]] = {"ref": "native"}
    self.assertEqual(self.select()["status"], "conflict")
    self.params.values.pop(SLOTS["qcom"])
    self.requests.write({"operation": {"id": "other", "action": "refresh", "status": "completed"}})
    self.assertEqual(self.select()["status"], "conflict")
    self.assertEqual(self.params.writes, [])

  def test_native_request_does_not_get_overwritten(self):
    self.params.values["ModelManager_DownloadRef"] = "native"
    self.assertEqual(self.select()["status"], "conflict")
    snapshot = self.remote.snapshot()
    self.assertEqual(snapshot["operation"]["ref"], "native")
    self.assertEqual(self.select(expected_operation_id=snapshot["operation"]["id"])["status"], "rejected")
    self.assertEqual(self.params.writes, [])

  def test_ambiguous_chestnut_ref_rejected_to_match_native_qcom_first_resolution(self):
    self.catalog[1]["ref"] = "small"
    self.assertEqual(self.select(source="chestnut")["status"], "rejected")
    self.assertEqual(self.params.writes, [])
    self.assertEqual(self.select()["status"], "accepted")

  def test_unknown_or_wrong_source_ref_rejected(self):
    self.assertEqual(self.select(ref="arbitrary-url")["status"], "rejected")
    self.assertEqual(self.select(source="chestnut")["status"], "rejected")
    self.assertEqual(self.select(source="chestnut", ref="big")["status"], "accepted")

  def test_marker_disappearance_requires_source_selection_readback(self):
    self.select()
    self.params.values.pop("ModelManager_DownloadRef")
    self.params.values[SLOTS["chestnut"]] = {"ref": "small"}
    self.assertEqual(self.remote.snapshot()["operation"]["status"], "unknown")

  def test_selection_completed_from_native_readback(self):
    operation = self.select()["snapshot"]["operation"]
    self.params.values[SLOTS["qcom"]] = {"ref": "small"}
    self.params.values.pop("ModelManager_DownloadRef")
    result = self.remote.snapshot()["operation"]
    self.assertEqual(result["id"], operation["id"])
    self.assertEqual(result["status"], "completed")

  def test_fast_native_selection_can_complete_in_command_response(self):
    def consume(key, value):
      if key == "ModelManager_DownloadRef":
        self.params.values.pop(key)
        self.params.values[SLOTS["qcom"]] = {"ref": value}
    self.params.after_write = consume
    result = self.select()
    self.assertEqual(result["status"], "accepted")
    self.assertEqual(result["snapshot"]["operation"]["status"], "completed")

  def test_fast_unobserved_consumption_is_accepted_but_unknown(self):
    self.params.after_write = lambda key, _value: self.params.values.pop(key)
    result = self.select()
    self.assertEqual(result["status"], "accepted")
    self.assertEqual(result["snapshot"]["operation"]["status"], "unknown")

  def test_new_native_marker_wins_readback_conflict(self):
    self.params.after_write = lambda key, _value: self.params.values.update({key: "native"})
    result = self.select()
    self.assertEqual(result["status"], "conflict")
    self.assertEqual(result["snapshot"]["operation"]["ref"], "native")

  def test_progress_is_only_from_matching_fresh_native_telemetry(self):
    self.select()
    self.live["selected"] = {"ref": "small", "status": "downloading", "models": [
      {"artifact": {"fileName": "model", "downloadProgress": {"status": "downloading", "progress": 45.5, "eta": 6}}},
      {"artifact": {"fileName": "model", "downloadProgress": {"progress": 90}}},
      {"artifact": {"fileName": "other", "downloadProgress": {"progress": math.nan}}},
    ]}
    snapshot = self.remote.snapshot()
    self.assertEqual(snapshot["operation"]["status"], "running")
    self.assertEqual(snapshot["operation"]["progress"], [
      {"name": "model", "status": "downloading", "percent": 45.5, "eta_seconds": 6},
      {"name": "other", "status": "notDownloading", "percent": 0}])
    json.dumps(snapshot, allow_nan=False)
    self.live["selected"]["ref"] = "different"
    self.assertNotIn("progress", self.remote.snapshot()["operation"])

  def test_observed_native_failure_retained_without_reidentifying_same_marker(self):
    first = self.select()["snapshot"]["operation"]
    self.live.update(selected={"ref": "small", "status": "failed"}, selected_at_ns=time.monotonic_ns())
    failed = self.remote.snapshot()["operation"]
    self.assertEqual(failed["status"], "failed")
    self.assertIn("failure or interruption", failed["message"])
    self.assertEqual(failed["id"], first["id"])
    self.live["selected"] = {}
    self.assertEqual(self.remote.snapshot()["operation"], failed)
    self.params.values.pop("ModelManager_DownloadRef")
    self.assertEqual(self.remote.snapshot()["operation"], failed)
    self.params.values["ModelManager_DownloadRef"] = "small"
    self.assertNotEqual(self.remote.snapshot()["operation"]["id"], first["id"])

  def test_previous_or_other_ref_failure_not_attributed_to_new_request(self):
    self.live.update(selected={"ref": "small", "status": "failed"}, selected_at_ns=time.monotonic_ns())
    self.assertEqual(self.select()["snapshot"]["operation"]["status"], "pending")
    self.live.update(selected={"ref": "other", "status": "failed"}, selected_at_ns=time.monotonic_ns())
    self.assertEqual(self.remote.snapshot()["operation"]["status"], "pending")

  def test_cancellation_failure_telemetry_does_not_claim_cancel_outcome(self):
    selected = self.select()["snapshot"]["operation"]
    self.remote.manage("cancel", operation_id=selected["id"])
    self.live.update(selected={"ref": "small", "status": "failed"}, selected_at_ns=time.monotonic_ns())
    self.assertEqual(self.remote.snapshot()["operation"]["status"], "unknown")

  def test_reset_changes_only_requested_slot_and_reads_back(self):
    self.params.values.update({SLOTS["qcom"]: {"ref": "small"}, SLOTS["chestnut"]: {"ref": "big"}})
    result = self.remote.manage("reset", source="qcom", expected_ref="small", expected_operation_id=None)
    self.assertEqual(result["status"], "saved")
    self.assertIsNone(self.params.get(SLOTS["qcom"]))
    self.assertEqual(self.params.get(SLOTS["chestnut"]), {"ref": "big"})
    self.assertEqual(result["snapshot"]["operation"]["status"], "completed")

  def test_reset_changed_readback_reports_conflict_without_rollback(self):
    self.params.after_write = lambda key, _value: self.params.values.update({key: {"ref": "native"}})
    result = self.remote.manage("reset", source="qcom", expected_ref=None, expected_operation_id=None)
    self.assertEqual(result["status"], "conflict")
    self.assertEqual(self.params.get(SLOTS["qcom"]), {"ref": "native"})

  def test_cancel_removes_marker_but_never_claims_cancellation_completed(self):
    selected = self.select()["snapshot"]["operation"]
    result = self.remote.manage("cancel", operation_id=selected["id"])
    self.assertEqual(result["status"], "accepted")
    self.assertEqual(result["snapshot"]["operation"]["id"], selected["id"])
    self.assertEqual(result["snapshot"]["operation"]["action"], "select")
    self.assertEqual(result["snapshot"]["operation"]["status"], "unknown")
    self.assertEqual(result["message"], "Cancellation request saved; the final outcome is unconfirmed.")
    self.assertEqual(result["snapshot"]["operation"]["message"],
                     "Cancellation request saved. The manager does not report a cancellation outcome.")
    self.assertIsNone(self.params.get("ModelManager_DownloadRef"))

  def test_cancelling_old_observation_does_not_remove_new_native_request(self):
    selected = self.select()["snapshot"]["operation"]
    self.params.values["ModelManager_DownloadRef"] = "native"
    self.assertEqual(self.remote.manage("cancel", operation_id=selected["id"])["status"], "conflict")
    self.assertEqual(self.params.get("ModelManager_DownloadRef"), "native")

  def test_same_ref_reappearing_after_cancellation_gets_new_observation(self):
    first = self.select()["snapshot"]["operation"]
    self.remote.manage("cancel", operation_id=first["id"])
    self.params.values["ModelManager_DownloadRef"] = "small"
    snapshot = self.remote.snapshot()
    second = snapshot["operation"]
    self.assertNotEqual(second["id"], first["id"])
    self.assertTrue(snapshot["actions"]["cancel"]["allowed"])
    self.assertEqual(self.remote.manage("cancel", operation_id=first["id"])["status"], "conflict")
    self.assertEqual(self.params.get("ModelManager_DownloadRef"), "small")
    self.assertEqual(self.remote.manage("cancel", operation_id=second["id"])["status"], "accepted")

  def test_same_ref_reappearing_after_completion_gets_new_observation(self):
    first = self.select()["snapshot"]["operation"]
    self.params.values[SLOTS["qcom"]] = {"ref": "small"}
    self.params.values.pop("ModelManager_DownloadRef")
    self.assertEqual(self.remote.snapshot()["operation"]["status"], "completed")
    self.params.values["ModelManager_DownloadRef"] = "small"
    self.assertNotEqual(self.remote.snapshot()["operation"]["id"], first["id"])

  def test_native_request_can_be_cancelled_without_prior_remote_receipt(self):
    self.params.values["ModelManager_DownloadRef"] = "small"
    first = self.remote.snapshot()["operation"]
    self.assertEqual(first["id"], self.remote.snapshot()["operation"]["id"])
    self.assertEqual(self.remote.manage("cancel", operation_id=first["id"])["status"], "accepted")

  def test_refresh_uses_native_sync_params_and_restamped_values(self):
    result = self.remote.manage("refresh", expected_operation_id=None)
    self.assertEqual(result["status"], "accepted")
    self.assertEqual(self.params.writes, [(key, 0) for key in SYNC_KEYS])
    self.params.values[SYNC_KEYS[0]] = time.monotonic_ns()
    self.assertEqual(self.remote.snapshot()["operation"]["status"], "pending")
    self.params.values[SYNC_KEYS[1]] = time.monotonic_ns()
    self.assertEqual(self.remote.snapshot()["operation"]["status"], "completed")

  def test_refresh_timeout_reports_unknown_instead_of_success(self):
    self.remote.manage("refresh", expected_operation_id=None)
    record = self.requests.read()
    record["operation"]["requested_ns"] -= 31_000_000_000
    self.requests.write(record)
    self.assertEqual(self.remote.snapshot()["operation"]["status"], "unknown")
    self.assertTrue(self.remote.snapshot()["actions"]["refresh"]["allowed"])

  def test_cache_clear_disappearance_is_not_success(self):
    result = self.remote.manage("clear_cache", expected_operation_id=None, confirmed=True)
    self.assertEqual(result["status"], "accepted")
    self.assertEqual(self.params.writes, [("ModelManager_ClearCache", True)])
    self.params.values.pop("ModelManager_ClearCache")
    self.assertEqual(self.remote.snapshot()["operation"]["status"], "unknown")

  def test_later_native_cache_clear_replaces_terminal_selection_and_keeps_identity(self):
    selected = self.select()["snapshot"]["operation"]
    self.params.values[SLOTS["qcom"]] = {"ref": "small"}
    self.params.values.pop("ModelManager_DownloadRef")
    self.assertEqual(self.remote.snapshot()["operation"]["status"], "completed")
    self.params.values["ModelManager_ClearCache"] = True
    clearing = self.remote.snapshot()["operation"]
    self.assertEqual(clearing["action"], "clear_cache")
    self.assertEqual(clearing["status"], "pending")
    self.assertNotEqual(clearing["id"], selected["id"])
    self.assertEqual(self.remote.snapshot()["operation"], clearing)
    self.params.values.pop("ModelManager_ClearCache")
    ended = self.remote.snapshot()["operation"]
    self.assertEqual(ended["id"], clearing["id"])
    self.assertEqual(ended["status"], "unknown")
    self.params.values["ModelManager_ClearCache"] = True
    self.assertNotEqual(self.remote.snapshot()["operation"]["id"], clearing["id"])

  def test_native_cache_clear_does_not_replace_active_download_observation(self):
    selected = self.select()["snapshot"]["operation"]
    self.params.values["ModelManager_ClearCache"] = True
    self.assertEqual(self.remote.snapshot()["operation"]["id"], selected["id"])
    self.params.values.pop("ModelManager_DownloadRef")
    self.live["selected"] = {"ref": "small", "status": "downloading"}
    self.assertEqual(self.remote.snapshot()["operation"]["id"], selected["id"])
    self.live["selected"] = {}
    self.assertEqual(self.remote.snapshot()["operation"]["action"], "clear_cache")

  def test_favorites_merge_unrelated_refs_and_verify_membership(self):
    self.params.values["ModelManager_Favs"] = "unknown;big"
    result = self.remote.manage("set_favorite", ref="small", enabled=True, expected_membership=False)
    self.assertEqual(result["status"], "saved")
    self.assertEqual(self.params.get("ModelManager_Favs"), "big;small;unknown")
    self.assertEqual(self.remote.manage("set_favorite", ref="small", enabled=False, expected_membership=False)["status"], "conflict")
    self.assertEqual(self.remote.manage("set_favorite", ref="unknown", enabled=False, expected_membership=True)["status"], "saved")

  def test_unknown_favorite_cannot_be_added(self):
    self.assertEqual(self.remote.manage("set_favorite", ref="unknown", enabled=True, expected_membership=False)["status"], "rejected")

  def test_favorites_concurrent_readback_conflict(self):
    self.params.after_write = lambda key, _value: self.params.values.update({key: "native"})
    result = self.remote.manage("set_favorite", ref="small", enabled=True, expected_membership=False)
    self.assertEqual(result["status"], "conflict")
    self.assertEqual(self.params.get("ModelManager_Favs"), "native")

  def test_invalid_fields_types_and_missing_confirmation_rejected(self):
    invalid = [("select", {"source": "qcom", "ref": "small", "expected_ref": None, "expected_operation_id": None, "url": "https://evil"}),
               ("select", {"source": "qcom", "ref": True, "expected_ref": None, "expected_operation_id": None}),
               ("select", {"source": "qcom", "ref": "a;b", "expected_ref": None, "expected_operation_id": None}),
               ("set_favorite", {"ref": "small", "enabled": 1, "expected_membership": False}),
               ("clear_cache", {"expected_operation_id": None, "confirmed": False}),
               ("cancel", {"operation_id": None}), ("refresh", {})]
    for action, kwargs in invalid:
      with self.subTest(action=action, kwargs=kwargs):
        self.assertEqual(self.remote.manage(action, **kwargs)["status"], "rejected")
    self.assertEqual(self.params.writes, [])

  def test_lost_process_receipt_never_replays_or_removes_native_params(self):
    first = self.select()["snapshot"]["operation"]
    self.remote.requests = RequestState()
    second = self.remote.snapshot()["operation"]
    self.assertNotEqual(first["id"], second["id"])
    self.assertEqual(self.params.writes, [("ModelManager_DownloadRef", "small")])
    self.assertEqual(self.params.get("ModelManager_DownloadRef"), "small")

  def test_submission_lock_failure_returns_unavailable_snapshot(self):
    with patch.object(self.requests, "locked", side_effect=WorkflowBusy("busy")):
      result = self.select()
    self.assertEqual(result["status"], "rejected")
    self.assertFalse(result["snapshot"]["available"])
    self.assertEqual(self.params.writes, [])

  def test_write_failure_is_not_retried(self):
    with patch.object(self.params, "put", side_effect=OSError("write failed")) as put:
      self.assertEqual(self.select()["status"], "failed")
    put.assert_called_once()


if __name__ == "__main__":
  unittest.main()
