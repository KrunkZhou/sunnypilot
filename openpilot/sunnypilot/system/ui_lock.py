"""Persistent UI-only locking shared by Athena and both native UI layouts.

This module has no Params, messaging, Sunnylink, or graphics dependencies. Remote
commands carry a verifier, never a PIN. Its sequence tombstone survives a local
unlock so replaying a delivered command cannot lock the screen again.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path


VERSION = 1
PIN_ITERATIONS = 600_000
MAX_MESSAGE_LENGTH = 80
MAX_SEQUENCE = 2 ** 63 - 1
ATTEMPT_INTERVAL = 2.0
MAX_STATE_BYTES = 8192
_STATE_KEYS = {"version", "sequence", "command_id", "command_hash", "revision", "locked", "message", "salt", "verifier"}


def _default_root() -> Path:
  if override := os.environ.get("RTZS_UI_LOCK_ROOT"):
    return Path(override)
  if Path("/AGNOS").is_file():
    return Path("/data/rtzs/ui_lock")
  return Path.home() / (".comma" + os.environ.get("OPENPILOT_PREFIX", "")) / "rtzs/ui_lock"


def _sequence(value, allow_zero=False) -> bool:
  return (type(value) is str and len(value) <= 19 and re.fullmatch(r"0|[1-9][0-9]*", value) is not None and
          (0 if allow_zero else 1) <= int(value) <= MAX_SEQUENCE)


def _hex(value, size: int) -> bool:
  return type(value) is str and re.fullmatch(f"[0-9a-f]{{{size * 2}}}", value) is not None


def _command_id(value) -> bool:
  if type(value) is not str:
    return False
  try:
    return str(uuid.UUID(value)) == value
  except ValueError:
    return False


def _message(value) -> bool:
  return (type(value) is str and len(value) <= MAX_MESSAGE_LENGTH and
          all(unicodedata.category(char) not in ("Cc", "Cs") for char in value))


def _empty_state() -> dict:
  return {"version": VERSION, "sequence": "0", "command_id": "", "command_hash": "", "revision": "0",
          "locked": False, "message": "", "salt": "", "verifier": ""}


def _valid_state(state) -> bool:
  if not isinstance(state, dict) or set(state) != _STATE_KEYS:
    return False
  if (type(state["version"]) is not int or state["version"] != VERSION or not _sequence(state["sequence"]) or
      not _command_id(state["command_id"]) or not _hex(state["command_hash"], 32) or not _sequence(state["revision"]) or
      type(state["locked"]) is not bool or not _message(state["message"])):
    return False
  if state["locked"]:
    return _hex(state["salt"], 16) and _hex(state["verifier"], 32)
  return state["message"] == state["salt"] == state["verifier"] == ""


class UiLockStore:
  """Synchronous storage API; callers keep disk work and PIN hashing off the UI thread."""
  def __init__(self, root: Path | str | None = None, *, clock=time.monotonic):
    self.root = Path(root) if root is not None else _default_root()
    self.path = self.root / "state.json"
    self.clock = clock
    self._attempt_lock = threading.Lock()
    self._next_attempt = -float("inf")

  def _read(self) -> tuple[dict, bool]:
    try:
      with self.path.open("rb") as stream:
        raw = stream.read(MAX_STATE_BYTES + 1)
      if len(raw) > MAX_STATE_BYTES:
        raise ValueError("Invalid UI lock state")
      state = json.loads(raw)
      if not _valid_state(state):
        raise ValueError("Invalid UI lock state")
      return state, False
    except FileNotFoundError:
      return _empty_state(), False
    except (OSError, ValueError, TypeError):
      state = _empty_state()
      state.update(locked=True)
      return state, True

  @staticmethod
  def _snapshot(state: dict, error=False) -> dict:
    return {"version": VERSION, "sequence": state["sequence"], "revision": state["revision"],
            "locked": state["locked"], "message": state["message"], "storage_error": error}

  def snapshot(self) -> dict:
    return self._snapshot(*self._read())

  @contextmanager
  def _locked(self):
    self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    self.root.chmod(0o700)
    fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
      fcntl.flock(fd, fcntl.LOCK_EX)
      yield
    finally:
      os.close(fd)

  def _write(self, state: dict) -> None:
    temporary = None
    try:
      with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, delete=False) as stream:
        temporary = stream.name
        json.dump(state, stream, ensure_ascii=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
      os.replace(temporary, self.path)
      directory = os.open(self.root, os.O_RDONLY)
      try:
        os.fsync(directory)
      finally:
        os.close(directory)
    finally:
      if temporary is not None:
        try:
          os.unlink(temporary)
        except FileNotFoundError:
          pass

  def apply(self, sequence: str, command_id: str, action: str, message: str = "", salt: str = "", verifier: str = "") -> dict:
    def result(status: str, snapshot=None):
      return {"version": VERSION, "status": status, "sequence": sequence if type(sequence) is str else "",
              "command_id": command_id if type(command_id) is str else "", "snapshot": snapshot or self.snapshot()}

    valid = (_sequence(sequence) and _command_id(command_id) and type(action) is str and action in ("lock", "unlock") and _message(message))
    if not valid or (action == "lock" and not (_hex(salt, 16) and _hex(verifier, 32))) or (action == "unlock" and (message, salt, verifier) != ("", "", "")):
      return result("failed")
    command = {"sequence": sequence, "command_id": command_id, "action": action, "message": message, "salt": salt, "verifier": verifier}
    digest = hashlib.sha256(json.dumps(command, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    try:
      with self._locked():
        state, error = self._read()
        if error and action != "unlock":
          return result("failed", self._snapshot(state, True))
        if int(sequence) < int(state["sequence"]):
          return result("superseded", self._snapshot(state, error))
        if sequence == state["sequence"]:
          same = command_id == state["command_id"] and hmac.compare_digest(digest, state["command_hash"])
          return result("duplicate" if same else "conflict", self._snapshot(state, error))
        if int(state["revision"]) == MAX_SEQUENCE:
          return result("failed", self._snapshot(state, error))
        state = {"version": VERSION, "sequence": sequence, "command_id": command_id, "command_hash": digest,
                 "revision": str(int(state["revision"]) + 1), "locked": action == "lock", "message": message, "salt": salt, "verifier": verifier}
        self._write(state)
        return result("applied", self._snapshot(state))
    except OSError:
      return result("failed")

  def verify_pin(self, pin: str, expected_revision: str) -> dict:
    def result(status, snapshot=None, retry_after=0.0):
      return {"status": status, "snapshot": snapshot or self.snapshot(), "retry_after": retry_after}

    state, error = self._read()
    if error:
      return result("failed", self._snapshot(state, True))
    if not state["locked"] or state["revision"] != expected_revision:
      return result("changed", self._snapshot(state))
    with self._attempt_lock:
      now = self.clock()
      if now < self._next_attempt:
        return result("throttled", self._snapshot(state), self._next_attempt - now)
      self._next_attempt = now + ATTEMPT_INTERVAL
    if type(pin) is not str or re.fullmatch(r"[0-9]{4,6}", pin) is None:
      return result("incorrect", self._snapshot(state))
    candidate = hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), bytes.fromhex(state["salt"]), PIN_ITERATIONS)
    correct = hmac.compare_digest(candidate, bytes.fromhex(state["verifier"]))
    try:
      with self._locked():
        current, error = self._read()
        if error:
          return result("failed", self._snapshot(current, True))
        if (current["revision"], current["sequence"], current["command_hash"]) != (expected_revision, state["sequence"], state["command_hash"]):
          return result("changed", self._snapshot(current))
        if not correct:
          return result("incorrect", self._snapshot(current))
        if int(current["revision"]) == MAX_SEQUENCE:
          return result("failed", self._snapshot(current))
        current.update(locked=False, message="", salt="", verifier="", revision=str(int(current["revision"]) + 1))
        self._write(current)
        return result("unlocked", self._snapshot(current))
    except OSError:
      return result("failed")


_STORE = None


def get_store() -> UiLockStore:
  global _STORE
  if _STORE is None:
    _STORE = UiLockStore()
  return _STORE


def get_device_ui_lock() -> dict:
  return get_store().snapshot()


def apply_device_ui_lock(sequence: str, command_id: str, action: str, message: str = "", salt: str = "", verifier: str = "") -> dict:
  return get_store().apply(sequence, command_id, action, message, salt, verifier)
