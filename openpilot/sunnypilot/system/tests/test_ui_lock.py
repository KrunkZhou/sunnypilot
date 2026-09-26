"""Portable persistence, replay, PIN-race, and Athena-boundary tests."""
import ast
import functools
import hashlib
import json
import os
import queue
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpilot.sunnypilot.system import ui_lock
from openpilot.system.athena.rpc import Dispatcher, handle


COMMAND_ID = "c350560e-1b15-4f5e-b3dc-d60bba84dc8b"
NEXT_ID = "0c43436a-b0eb-4c47-ad5d-4b16a7b84c03"
SALT = "f1288fb128c297075d73e3ce28f67b46"
PIN = "0123"
VERIFIER = hashlib.pbkdf2_hmac("sha256", PIN.encode(), bytes.fromhex(SALT), 600_000).hex()
ROOT = Path(__file__).resolve().parents[4]
ATHENA = ROOT / "openpilot/system/athena/athenad.py"


class TestUiLockStore(unittest.TestCase):
  def setUp(self):
    self.temporary = tempfile.TemporaryDirectory()
    self.addCleanup(self.temporary.cleanup)
    self.root = Path(self.temporary.name) / "ui_lock"
    self.now = 10.0
    self.store = ui_lock.UiLockStore(self.root, clock=lambda: self.now)

  def lock(self, **kwargs):
    command = {"sequence": "1", "command_id": COMMAND_ID, "action": "lock", "message": "Return to owner", "salt": SALT, "verifier": VERIFIER}
    return self.store.apply(**(command | kwargs))

  def test_empty_snapshot_does_not_create_storage(self):
    self.assertEqual(self.store.snapshot(), {"version": 1, "sequence": "0", "revision": "0", "locked": False, "message": "", "storage_error": False})
    self.assertFalse(self.root.exists())

  def test_fixed_verifier_and_leading_zero_pin(self):
    locked = self.lock()["snapshot"]
    self.assertEqual(locked["revision"], "1")
    self.assertEqual(self.store.verify_pin("123", locked["revision"])["status"], "incorrect")
    self.now += 2
    unlocked = self.store.verify_pin(PIN, locked["revision"])
    self.assertEqual(unlocked["status"], "unlocked")
    self.assertFalse(unlocked["snapshot"]["locked"])
    self.assertEqual(unlocked["snapshot"]["revision"], "2")
    self.assertEqual(unlocked["snapshot"]["sequence"], "1")
    self.assertEqual(ui_lock.PIN_ITERATIONS, 600_000)

  def test_persistence_permissions_and_public_secret_omission(self):
    applied = self.lock()
    self.assertEqual(applied["status"], "applied")
    self.assertEqual(ui_lock.UiLockStore(self.root).snapshot(), applied["snapshot"])
    self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
    self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
    state = json.loads(self.store.path.read_text())
    for secret in (PIN, SALT, VERIFIER, state["command_hash"]):
      self.assertNotIn(secret, json.dumps(applied))
    self.assertNotIn('"pin"', self.store.path.read_text())

  def test_retry_after_local_unlock_returns_current_unlocked_state(self):
    applied = self.lock()
    self.store.verify_pin(PIN, applied["snapshot"]["revision"])
    before = self.store.path.read_bytes()
    duplicate = self.lock()
    self.assertEqual(duplicate["status"], "duplicate")
    self.assertFalse(duplicate["snapshot"]["locked"])
    self.assertEqual(duplicate["snapshot"]["revision"], "2")
    self.assertEqual(before, self.store.path.read_bytes())
    state = json.loads(before)
    self.assertEqual((state["message"], state["salt"], state["verifier"]), ("", "", ""))
    self.assertEqual(state["command_id"], COMMAND_ID)

  def test_restart_preserves_unlock_and_sequence_tombstone(self):
    applied = self.lock()
    self.store.verify_pin(PIN, applied["snapshot"]["revision"])
    command = {"sequence": "1", "command_id": COMMAND_ID, "action": "lock", "message": "Return to owner", "salt": SALT, "verifier": VERIFIER}
    code = ("import json, sys; from openpilot.sunnypilot.system.ui_lock import UiLockStore; "
            "print(json.dumps(UiLockStore(sys.argv[1]).apply(**json.loads(sys.argv[2]))))")
    process = subprocess.run([sys.executable, "-c", code, str(self.root), json.dumps(command)], cwd=ROOT, capture_output=True, text=True, check=True)
    restarted = json.loads(process.stdout)
    self.assertEqual(restarted["status"], "duplicate")
    self.assertFalse(restarted["snapshot"]["locked"])

  def test_delayed_lock_cannot_override_newer_remote_unlock(self):
    self.lock()
    cleared = self.store.apply("3", NEXT_ID, "unlock")
    self.assertEqual(cleared["status"], "applied")
    delayed = self.lock(sequence="2")
    self.assertEqual(delayed["status"], "superseded")
    self.assertFalse(delayed["snapshot"]["locked"])
    self.assertEqual(delayed["snapshot"]["sequence"], "3")

  def test_reused_sequence_with_changed_id_or_payload_conflicts(self):
    self.lock()
    before = self.store.path.read_bytes()
    for change in ({"command_id": NEXT_ID}, {"message": "Changed"}, {"salt": "00" * 16}, {"verifier": "00" * 32}):
      with self.subTest(change=tuple(change)):
        self.assertEqual(self.lock(**change)["status"], "conflict")
        self.assertEqual(self.store.path.read_bytes(), before)

  def test_validation_rejects_noncanonical_or_unbounded_values_without_writes(self):
    invalid = [
      {"sequence": value} for value in (0, True, "0", "01", "+1", " 1", "-1", "1.0", str(2 ** 63), "1" * 100)
    ] + [{"command_id": value} for value in (None, "invalid", COMMAND_ID.upper(), COMMAND_ID.replace("-", ""))]
    invalid += [{"action": "erase"}, {"message": "x" * 81}, {"message": "two\nlines"}, {"message": "\ud800"},
                {"salt": SALT.upper()}, {"salt": "ff"}, {"verifier": VERIFIER.upper()}, {"verifier": "ff"}, {"verifier": None}]
    for change in invalid:
      with self.subTest(change=repr(change)):
        self.assertEqual(self.lock(**change)["status"], "failed")
        self.assertFalse(self.root.exists())
    self.assertEqual(self.lock(message="界" * 80)["status"], "applied")

  def test_unlock_rejects_secret_or_message_fields(self):
    for key in ("salt", "verifier", "message"):
      with self.subTest(key=key):
        self.assertEqual(self.store.apply("1", COMMAND_ID, "unlock", **{key: "unexpected"})["status"], "failed")
    self.assertFalse(self.root.exists())

  def test_wrong_pin_and_unicode_digits_never_unlock(self):
    revision = self.lock()["snapshot"]["revision"]
    for pin in ("9876", "０１２３", "١٢٣٤", "0123 ", "0123456", 1234):
      self.now += 2
      with self.subTest(pin=pin):
        self.assertEqual(self.store.verify_pin(pin, revision)["status"], "incorrect")
        self.assertTrue(self.store.snapshot()["locked"])

  def test_throttle_does_not_reset_between_calls_or_on_stale_input(self):
    revision = self.lock()["snapshot"]["revision"]
    self.assertEqual(self.store.verify_pin("9999", revision)["status"], "incorrect")
    self.assertEqual(self.store.verify_pin(PIN, "0")["status"], "changed")
    throttled = self.store.verify_pin(PIN, revision)
    self.assertEqual(throttled["status"], "throttled")
    self.assertEqual(throttled["retry_after"], 2)
    self.now += 2
    self.assertEqual(self.store.verify_pin(PIN, revision)["status"], "unlocked")

  def test_new_lock_wins_over_pending_pin_verification(self):
    revision = self.lock()["snapshot"]["revision"]
    derive = hashlib.pbkdf2_hmac

    def replace_lock(*args, **kwargs):
      self.assertEqual(self.lock(sequence="2", command_id=NEXT_ID, message="New lock")["status"], "applied")
      return derive(*args, **kwargs)

    with patch.object(ui_lock.hashlib, "pbkdf2_hmac", side_effect=replace_lock):
      result = self.store.verify_pin(PIN, revision)
    self.assertEqual(result["status"], "changed")
    self.assertTrue(result["snapshot"]["locked"])
    self.assertEqual(result["snapshot"]["message"], "New lock")

  def test_remote_unlock_wins_over_pending_pin_verification(self):
    revision = self.lock()["snapshot"]["revision"]
    derive = hashlib.pbkdf2_hmac

    def clear_lock(*args, **kwargs):
      self.store.apply("2", NEXT_ID, "unlock")
      return derive(*args, **kwargs)

    with patch.object(ui_lock.hashlib, "pbkdf2_hmac", side_effect=clear_lock):
      result = self.store.verify_pin(PIN, revision)
    self.assertEqual(result["status"], "changed")
    self.assertFalse(result["snapshot"]["locked"])

  def test_atomic_failure_keeps_previous_record_and_retry_is_safe(self):
    self.lock()
    before = self.store.path.read_bytes()
    with patch.object(ui_lock.os, "replace", side_effect=OSError("write failed")):
      self.assertEqual(self.store.apply("2", NEXT_ID, "unlock")["status"], "failed")
    self.assertEqual(self.store.path.read_bytes(), before)
    self.assertEqual({path.name for path in self.root.iterdir()}, {".lock", "state.json"})
    self.assertEqual(self.store.apply("2", NEXT_ID, "unlock")["status"], "applied")
    self.assertEqual(self.store.apply("2", NEXT_ID, "unlock")["status"], "duplicate")

  def test_unreadable_storage_fails_locked(self):
    with patch.object(Path, "open", side_effect=PermissionError("denied")):
      snapshot = self.store.snapshot()
      self.assertTrue(snapshot["locked"])
      self.assertTrue(snapshot["storage_error"])
      self.assertEqual(self.store.verify_pin(PIN, "0")["status"], "failed")

  def test_corrupt_storage_fails_locked_and_remote_clear_recovers(self):
    self.root.mkdir()
    for invalid in ("{", "{}", "x" * (ui_lock.MAX_STATE_BYTES + 1)):
      self.store.path.write_text(invalid)
      snapshot = self.store.snapshot()
      self.assertTrue(snapshot["locked"])
      self.assertTrue(snapshot["storage_error"])
      self.assertEqual(self.lock()["status"], "failed")
    repaired = self.store.apply("2", NEXT_ID, "unlock")
    self.assertEqual(repaired["status"], "applied")
    self.assertFalse(repaired["snapshot"]["storage_error"])
    self.assertEqual(self.lock(sequence="3")["status"], "applied")

  def test_parallel_store_instances_keep_highest_sequence(self):
    stores = [ui_lock.UiLockStore(self.root) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
      futures = [executor.submit(store.apply, str(i + 1), COMMAND_ID, "lock", str(i), SALT, VERIFIER) for i, store in enumerate(stores)]
      results = [future.result() for future in futures]
    self.assertTrue(all(result["status"] in ("applied", "superseded") for result in results))
    self.assertEqual(self.store.snapshot()["sequence"], "8")
    self.assertEqual(self.store.snapshot()["message"], "7")

  def test_default_desktop_prefix_and_explicit_override(self):
    with patch.dict(os.environ, {"RTZS_UI_LOCK_ROOT": "/tmp/test-ui-lock"}):
      self.assertEqual(ui_lock.UiLockStore().root, Path("/tmp/test-ui-lock"))
    with patch.dict(os.environ, {"OPENPILOT_PREFIX": "-test"}, clear=True), patch.object(Path, "is_file", return_value=False):
      self.assertEqual(ui_lock.UiLockStore().root, Path.home() / ".comma-test/rtzs/ui_lock")


class TestUiLockAthenaBoundary(unittest.TestCase):
  def setUp(self):
    self.tree = ast.parse(ATHENA.read_text())

  def test_methods_are_registered_only_when_primary_athena_starts(self):
    names = {"getDeviceUiLock", "applyDeviceUiLock", "register_ui_lock_methods"}
    wrappers = [node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    self.assertEqual(len(wrappers), 3)
    self.assertTrue(all(not node.decorator_list for node in wrappers))
    dispatcher = Dispatcher()
    namespace = {"dispatcher": dispatcher}
    exec(compile(ast.Module(body=wrappers, type_ignores=[]), str(ATHENA), "exec"), namespace)
    self.assertEqual(dispatcher, {})
    namespace["register_ui_lock_methods"]()
    self.assertEqual(set(dispatcher), names - {"register_ui_lock_methods"})
    with patch.object(ui_lock, "get_device_ui_lock", return_value={"version": 1}) as getter:
      self.assertEqual(dispatcher["getDeviceUiLock"](), {"version": 1})
      getter.assert_called_once_with()
    with patch.object(ui_lock, "apply_device_ui_lock", return_value={"status": "applied"}) as apply:
      self.assertEqual(dispatcher["applyDeviceUiLock"]("1", COMMAND_ID, "lock", "Message", SALT, VERIFIER), {"status": "applied"})
      apply.assert_called_once_with("1", COMMAND_ID, "lock", "Message", SALT, VERIFIER)
    main = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    self.assertEqual(ast.unparse(main.body[0]), "register_ui_lock_methods()")
    top_level_calls = [node for node in self.tree.body if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)]
    self.assertFalse(any("register_ui_lock_methods" in ast.unparse(node) for node in top_level_calls))

  def test_unknown_lock_method_logs_no_verifier_or_custom_text(self):
    handler = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == "jsonrpc_handler")
    for method in ("getDeviceUiLock", "applyDeviceUiLock"):
      with self.subTest(method=method):
        end = threading.Event()
        pending = [json.dumps({"method": method, "params": {"message": "private message", "salt": SALT, "verifier": VERIFIER}, "id": 1})]

        def receive(**kwargs):
          if pending:
            return pending.pop()
          end.set()
          raise queue.Empty

        log = Mock()
        namespace = {"threading": threading, "dispatcher": {}, "partial": functools.partial, "startLocalProxy": Mock(),
                     "recv_queue": SimpleNamespace(get=receive), "loads": json.loads, "is_call": lambda message: "method" in message,
                     "cloudlog": log, "send_queue_push": Mock(), "handle": handle, "SEND_PRIORITY_HIGH": 0, "queue": queue}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), str(ATHENA), "exec"), namespace)
        namespace["jsonrpc_handler"](end)
        log.event.assert_called_once_with("athena.jsonrpc_handler.call_method", method=method)
        response = json.loads(namespace["send_queue_push"].call_args.args[0])
        self.assertEqual(response["error"]["code"], -32601)
        self.assertNotIn(VERIFIER, json.dumps(response))


if __name__ == "__main__":
  unittest.main()
