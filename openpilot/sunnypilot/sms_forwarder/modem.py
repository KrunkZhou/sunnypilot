from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import select
import termios
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal


AT_PORT = "/dev/modem_at0"
AT_LOCK = "/dev/shm/modem.lock"
MODEM_STATE_PATH = "/dev/shm/modem"

_CMGL_HEADER = re.compile(r"^\+CMGL:\s*(\d+),")
_HEX_LINE = re.compile(r"^[0-9A-Fa-f]+$")


@dataclass(frozen=True)
class ATResult:
  ok: bool
  lines: tuple[str, ...]


@dataclass(frozen=True)
class StoredPDU:
  storage: str
  index: int
  raw_pdu: str
  digest: str


@dataclass(frozen=True)
class PDURead:
  status: Literal["retry", "missing", "found"]
  raw_pdu: str = ""


class ATClient:
  def __init__(self, port: str = AT_PORT, lock_path: str = AT_LOCK):
    self.port = port
    self.lock_path = lock_path

  def command(self, command: str, timeout: float = 5.0) -> ATResult | None:
    lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o666)
    try:
      try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
      except OSError:
        return None
      if not os.path.exists(self.port):
        return None
      try:
        with _serial_port(self.port, 9600) as serial_fd:
          termios.tcflush(serial_fd, termios.TCIFLUSH)
          pending = (command + "\r").encode()
          while pending:
            pending = pending[os.write(serial_fd, pending):]
          lines = []
          while True:
            raw = _read_line(serial_fd, timeout)
            if not raw:
              return None
            line = raw.decode(errors="ignore").strip()
            if not line or line == command:
              continue
            if line == "OK":
              return ATResult(ok=True, lines=tuple(lines))
            if line == "ERROR" or line.startswith(("+CME ERROR", "+CMS ERROR")):
              return ATResult(ok=False, lines=tuple(lines))
            lines.append(line)
      except OSError:
        return None
    finally:
      try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
      finally:
        os.close(lock_fd)


class SMSModem:
  def __init__(self, client: ATClient | None = None, state_path: str = MODEM_STATE_PATH):
    self.client = client or ATClient()
    self.state_path = state_path

  def current_iccid(self) -> str:
    try:
      with open(self.state_path) as state_file:
        value = json.load(state_file).get("iccid", "")
      return value if isinstance(value, str) and value.isdigit() else ""
    except (FileNotFoundError, json.JSONDecodeError, OSError):
      return ""

  def scan(self) -> list[StoredPDU] | None:
    mode = self.client.command("AT+CMGF=0")
    if mode is None or not mode.ok:
      return None
    messages = []
    for storage in ("SM", "ME"):
      selected = self.client.command(f'AT+CPMS="{storage}"')
      if selected is None or not selected.ok:
        continue
      response = self.client.command("AT+CMGL=4")
      if response is None:
        return None
      if response.ok:
        messages.extend(_parse_cmgl(storage, response.lines))
    return messages

  def read(self, storage: str, index: int) -> PDURead:
    if storage not in ("SM", "ME") or index < 0:
      return PDURead("missing")
    mode = self.client.command("AT+CMGF=0")
    if mode is None or not mode.ok:
      return PDURead("retry")
    selected = self.client.command(f'AT+CPMS="{storage}"')
    if selected is None or not selected.ok:
      return PDURead("retry")
    response = self.client.command(f"AT+CMGR={index}")
    if response is None:
      return PDURead("retry")
    if not response.ok:
      return PDURead("missing")
    raw_pdu = _find_pdu(response.lines)
    return PDURead("found", raw_pdu) if raw_pdu else PDURead("missing")

  def delete(self, storage: str, index: int) -> bool:
    if storage not in ("SM", "ME") or index < 0:
      return False
    selected = self.client.command(f'AT+CPMS="{storage}"')
    if selected is None or not selected.ok:
      return False
    response = self.client.command(f"AT+CMGD={index}")
    return response is not None and response.ok


def pdu_digest(raw_pdu: str) -> str:
  compact = "".join(raw_pdu.split()).upper()
  return hashlib.sha256(bytes.fromhex(compact)).hexdigest()


def _parse_cmgl(storage: str, lines: tuple[str, ...]) -> list[StoredPDU]:
  messages = []
  for position, line in enumerate(lines[:-1]):
    match = _CMGL_HEADER.match(line)
    if match is None:
      continue
    raw_pdu = lines[position + 1].strip()
    if len(raw_pdu) % 2 != 0 or _HEX_LINE.fullmatch(raw_pdu) is None:
      continue
    try:
      digest = pdu_digest(raw_pdu)
    except ValueError:
      continue
    messages.append(StoredPDU(storage=storage, index=int(match.group(1)), raw_pdu=raw_pdu.upper(), digest=digest))
  return messages


def _find_pdu(lines: tuple[str, ...]) -> str:
  for position, line in enumerate(lines[:-1]):
    if line.startswith("+CMGR:"):
      candidate = lines[position + 1].strip()
      if len(candidate) % 2 == 0 and _HEX_LINE.fullmatch(candidate) is not None:
        return candidate.upper()
  return ""


@contextmanager
def _serial_port(port: str, baudrate: int):
  file_descriptor = os.open(port, os.O_RDWR | os.O_NOCTTY)
  try:
    attrs = termios.tcgetattr(file_descriptor)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    attrs[3] = 0
    attrs[4] = attrs[5] = getattr(termios, f"B{baudrate}")
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(file_descriptor, termios.TCSANOW, attrs)
    yield file_descriptor
  finally:
    os.close(file_descriptor)


def _read_line(file_descriptor: int, timeout: float) -> bytes:
  data = bytearray()
  deadline = time.monotonic() + timeout
  while True:
    readable, _, _ = select.select([file_descriptor], [], [], max(0.0, deadline - time.monotonic()))
    if not readable:
      return bytes(data)
    value = os.read(file_descriptor, 1)
    if not value:
      return bytes(data)
    data.extend(value)
    if value == b"\n":
      return bytes(data)
