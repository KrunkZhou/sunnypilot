"""Portable AssistNow receiver discovery, proxy protocol, and UBX flow control.

This module has no hardware dependencies. Only pigeond's receiving thread may
drive ReceiverProbe or MGAInjector; the HTTP worker communicates through Session.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, UTC
from email.utils import parsedate_to_datetime
import json
import math
import queue
import re
import threading
import time


PROTOCOL_HEADER = "X-Assist-Protocol"
PROTOCOL_VERSION = "ubx-v1"
MAX_UBX_PAYLOAD = 4096
MAX_ASSIST_BYTES = 256 * 1024
MAX_ASSIST_MESSAGES = 2048
MAX_BATCH_AGE = 5.0
MAX_RETRY_AFTER = 30 * 24 * 60 * 60
RECEIVER_MESSAGES = {b"\x27\x03": "UBX-SEC-UNIQID", b"\x0a\x04": "UBX-MON-VER"}


def checksum(data: bytes) -> bytes:
  a = b = 0
  for value in data:
    a = (a + value) & 0xff
    b = (b + a) & 0xff
  return bytes((a, b))


def ubx_message(message_id: bytes, payload: bytes = b"") -> bytes:
  body = message_id + len(payload).to_bytes(2, "little") + payload
  return b"\xb5\x62" + body + checksum(body)


def valid_frame(frame: bytes) -> bool:
  return (8 <= len(frame) <= MAX_UBX_PAYLOAD + 8 and frame[:2] == b"\xb5\x62" and
          int.from_bytes(frame[4:6], "little") + 8 == len(frame) and checksum(frame[2:-2]) == frame[-2:])


def parse_assistance(data: bytes) -> list[bytes]:
  if not data or len(data) > MAX_ASSIST_BYTES:
    raise ValueError("invalid assistance size")
  messages = []
  offset = 0
  while offset < len(data):
    if len(data) - offset < 8:
      raise ValueError("truncated assistance")
    size = int.from_bytes(data[offset + 4:offset + 6], "little") + 8
    frame = data[offset:offset + size]
    if not valid_frame(frame) or frame[2] != 0x13 or frame[3] == 0x60 or len(frame) < 12:
      raise ValueError("invalid assistance frame")
    messages.append(frame)
    if len(messages) > MAX_ASSIST_MESSAGES:
      raise ValueError("too many assistance messages")
    offset += size
  return messages


class UbxStream:
  """Bounded incremental decoder; the caller retains and publishes the raw data."""
  def __init__(self):
    self.buffer = bytearray()

  def feed(self, data: bytes) -> list[bytes]:
    self.buffer.extend(data)
    frames = []
    while len(self.buffer) >= 2:
      start = self.buffer.find(b"\xb5\x62")
      if start < 0:
        self.buffer[:] = self.buffer[-1:] if self.buffer[-1] == 0xb5 else b""
        break
      del self.buffer[:start]
      if len(self.buffer) < 6:
        break
      size = int.from_bytes(self.buffer[4:6], "little") + 8
      if size > MAX_UBX_PAYLOAD + 8:
        del self.buffer[0]
        continue
      if len(self.buffer) < size:
        break
      frame = bytes(self.buffer[:size])
      if valid_frame(frame):
        frames.append(frame)
        del self.buffer[:size]
      else:
        del self.buffer[0]
    return frames


class ReceiverProbe:
  TIMEOUT = 1.0

  def __init__(self, now: float):
    self.deadline = now + self.TIMEOUT
    self.next_poll = now
    self.messages: dict[str, str] = {}

  def feed(self, frame: bytes) -> None:
    if not valid_frame(frame):
      return
    message_id = frame[2:4]
    payload = frame[6:-2]
    # SEC-UNIQID versions 1/2 have five/six unique ID bytes after the
    # version/reserved fields. MON-VER has fixed 30+10 byte fields and
    # optional 30-byte extension strings. Preserve the full response for ZTP.
    if message_id == b"\x27\x03":
      if (len(payload), payload[0] if payload else 0) not in ((9, 1), (10, 2)) or payload[1:4] != b"\x00\x00\x00":
        return
      if payload[4:] in (bytes(len(payload) - 4), b"\xff" * (len(payload) - 4)):
        return
    elif message_id == b"\x0a\x04":
      if (len(payload) < 40 or (len(payload) - 40) % 30 or any(value != 0 and not 32 <= value <= 126 for value in payload) or
          not payload[:30].split(b"\x00", 1)[0].strip() or not payload[30:40].split(b"\x00", 1)[0].strip()):
        return
    else:
      return
    self.messages[RECEIVER_MESSAGES[message_id]] = frame.hex()

  @property
  def complete(self) -> bool:
    return len(self.messages) == len(RECEIVER_MESSAGES)

  def poll(self, now: float) -> list[bytes]:
    if self.complete or now >= self.deadline or now < self.next_poll:
      return []
    self.next_poll = now + 0.5
    return [ubx_message(message_id) for message_id, name in RECEIVER_MESSAGES.items() if name not in self.messages]


def mga_ack_result(frame: bytes, sent: bytes) -> bool | None:
  """Correlate MGA-ACK-DATA0 with message ID and its first four payload bytes.

  See u-blox M9 SPG 4.04 Interface description, UBX-MGA-ACK-DATA0.
  """
  if not valid_frame(frame):
    return None
  payload = frame[6:-2]
  if frame[2:4] == b"\x05\x00" and payload == sent[2:4]:
    return False
  if frame[2:4] != b"\x13\x60" or len(payload) != 8:
    return None
  if payload[3] != sent[3] or payload[4:8] != sent[6:10]:
    return None
  return payload[:3] == b"\x01\x00\x00"


class MGAInjector:
  ACK_TIMEOUT = 0.5
  MAX_ATTEMPTS = 2
  MAX_DURATION = 30.0

  def __init__(self, messages: list[bytes], now: float):
    # Also validate callers which supply frames without going through HTTP.
    self.messages = parse_assistance(b"".join(messages))
    self.index = 0
    self.attempts = 0
    self.sent_at: float | None = None
    self.deadline = now + self.MAX_DURATION
    self.error: str | None = None

  @property
  def done(self) -> bool:
    return self.error is not None or self.index == len(self.messages)

  def feed(self, frame: bytes) -> None:
    if self.done or self.sent_at is None:
      return
    result = mga_ack_result(frame, self.messages[self.index])
    if result is True:
      self.index += 1
      self.attempts = 0
      self.sent_at = None
    elif result is False:
      self.error = "receiver rejected assistance"

  def poll(self, now: float) -> bytes | None:
    if self.done:
      return None
    if now >= self.deadline:
      self.error = "assistance injection deadline exceeded"
      return None
    if self.sent_at is not None and now - self.sent_at < self.ACK_TIMEOUT:
      return None
    # An absolute time message describes when it was generated. Replaying the
    # same bytes after a timeout can set the receiver's clock backwards.
    message = self.messages[self.index]
    is_absolute_time = message[2:4] == b"\x13\x40" and message[6] in (0x10, 0x11)
    max_attempts = 1 if is_absolute_time else self.MAX_ATTEMPTS
    if self.attempts >= max_attempts:
      self.error = "assistance acknowledgement timed out"
      return None
    self.attempts += 1
    self.sent_at = now
    return self.messages[self.index]


@dataclass(frozen=True)
class AssistanceBatch:
  messages: list[bytes]
  received_at: float


class Session:
  """A distinct object per receiver initialization fences late worker replies."""
  def __init__(self):
    self.cancelled = threading.Event()
    self.probe_requested = threading.Event()
    self.identity_ready = threading.Event()
    self.identity: dict[str, str] | None = None
    self.batches: queue.Queue[AssistanceBatch] = queue.Queue(maxsize=1)

  def request_identity(self) -> None:
    self.identity = None
    self.identity_ready.clear()
    self.probe_requested.set()

  def set_identity(self, identity: dict[str, str] | None) -> None:
    if not self.cancelled.is_set():
      self.identity = identity
      self.identity_ready.set()

  def set_messages(self, messages: list[bytes], received_at: float | None = None) -> None:
    if not self.cancelled.is_set():
      self.batches.put_nowait(AssistanceBatch(messages, time.monotonic() if received_at is None else received_at))


class UARTAssistance:
  """Advance probes and injection without waiting or consuming navigation bytes."""
  def __init__(self, session: Session):
    self.session = session
    self.stream = UbxStream()
    self.probe: ReceiverProbe | None = None
    self.injector: MGAInjector | None = None

  @property
  def busy(self) -> bool:
    return self.probe is not None or self.injector is not None

  @property
  def partial_frame(self) -> bool:
    return bool(self.stream.buffer)

  def send_failed(self) -> None:
    if self.probe is not None:
      self.session.set_identity(None)
      self.probe = None
    self.injector = None

  def update(self, data: bytes, now: float) -> tuple[list[bytes], list[str]]:
    outgoing: list[bytes] = []
    status: list[str] = []
    if self.session.cancelled.is_set():
      return outgoing, status
    for frame in self.stream.feed(data):
      if self.probe is not None:
        self.probe.feed(frame)
      if self.injector is not None:
        self.injector.feed(frame)

    if self.probe is not None and (self.probe.complete or now >= self.probe.deadline):
      self.session.set_identity(dict(self.probe.messages) if self.probe.complete else None)
      self.probe = None
    if self.probe is None and self.session.probe_requested.is_set():
      self.session.probe_requested.clear()
      self.probe = ReceiverProbe(now)
    if self.probe is not None:
      outgoing.extend(self.probe.poll(now))

    if self.injector is None:
      try:
        batch = self.session.batches.get_nowait()
        if now - batch.received_at > MAX_BATCH_AGE:
          status.append("discarded stale AssistNow messages")
        else:
          self.injector = MGAInjector(batch.messages, now)
      except queue.Empty:
        pass
      except ValueError:
        status.append("failed to validate AssistNow messages")
    if self.injector is not None:
      if message := self.injector.poll(now):
        outgoing.append(message)
      if self.injector.done:
        status.append(self.injector.error or "AssistNow messages acknowledged by receiver")
        self.injector = None
    return outgoing, status


class RetryLater(Exception):
  def __init__(self, message: str, delay: float = 10.0, code: str | None = None):
    super().__init__(message)
    self.delay = delay
    self.code = code


def retry_delay(response, default: float = 10.0) -> float:
  value = response.headers.get("Retry-After", "")
  try:
    delay = float(value)
  except ValueError:
    try:
      delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
    except (ValueError, TypeError, OverflowError):
      delay = default
  if not math.isfinite(delay):
    delay = default
  return min(float(MAX_RETRY_AFTER), max(1.0, delay))


def response_error(response, message: str, default_delay: float = 10.0) -> RetryLater:
  # Interpret only a small machine-readable code. Never log a provider/server
  # body or its free-form message, which may contain credentials or identity.
  code = None
  if len(response.content) <= 4096:
    try:
      payload = json.loads(response.content)
      value = payload.get("error") if isinstance(payload, dict) else None
      if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        code = value
    except (ValueError, TypeError):
      pass
  return RetryLater(message, retry_delay(response, default_delay), code)


class _NoRedirectHTTP:
  def request(self, *args, **kwargs):
    import requests
    return requests.request(*args, **kwargs, allow_redirects=False)


class AssistNowClient:
  """Optional identity handshake on the same backend as the legacy assist API."""
  def __init__(self, api, dongle_id: str):
    self.api = api
    self.endpoint = f"v1/{dongle_id}/assist"
    self.receiver_http = _NoRedirectHTTP()

  def _get(self, endpoint: str):
    return self.api.get(endpoint, access_token=self.api.get_token(), timeout=5)

  def supports_receiver(self) -> bool:
    response = self.api.get(self.endpoint + "/receiver", access_token=self.api.get_token(), timeout=5, session=self.receiver_http)
    protocol = response.headers.get(PROTOCOL_HEADER)
    if response.status_code == 200 and protocol == PROTOCOL_VERSION:
      return True
    if not protocol and (response.status_code in (404, 405) or 200 <= response.status_code < 300):
      return False
    raise response_error(response, "receiver capability unavailable")

  def register_receiver(self, messages: dict[str, str]) -> None:
    response = self.api.post(self.endpoint + "/receiver", json={"messages": messages},
                             access_token=self.api.get_token(), timeout=5, session=self.receiver_http)
    if response.status_code not in (200, 202):
      raise response_error(response, "receiver registration unavailable", 60.0)

  def fetch(self) -> list[bytes]:
    response = self._get(self.endpoint)
    if response.status_code != 200:
      default = 60.0 if response.status_code in (401, 403, 422) else 10.0
      raise response_error(response, f"assistance unavailable (HTTP {response.status_code})", default)
    return parse_assistance(response.content)


def download(session: Session, client_factory: Callable[[], AssistNowClient],
             direct_fetch: Callable[[], list[bytes]] | None, network_ready: Callable[[], bool],
             warn: Callable[[str], None]) -> None:
  """HTTP worker. No method here reads or writes the receiver's UART."""
  client = None
  receiver_supported: bool | None = None
  registered = False
  failures = 0
  while not session.cancelled.is_set():
    try:
      if not network_ready():
        session.cancelled.wait(1.0)
        continue
      if session.cancelled.is_set():
        return
      if direct_fetch is not None:
        messages = direct_fetch()
      else:
        if client is None:
          client = client_factory()
        if receiver_supported is None:
          receiver_supported = client.supports_receiver()
        if session.cancelled.is_set():
          return
        if receiver_supported and not registered:
          if session.identity is None:
            session.request_identity()
            while not session.identity_ready.wait(0.1):
              if session.cancelled.is_set():
                return
            if session.identity is None:
              raise RetryLater("receiver identity unavailable", 60.0)
          if session.cancelled.is_set():
            return
          client.register_receiver(session.identity)
          registered = True
        if session.cancelled.is_set():
          return
        messages = client.fetch()
      session.set_messages(messages)
      return
    except Exception as error:
      # Never log request URLs, receiver identity, credentials, or provider bodies.
      failures += 1
      if isinstance(error, RetryLater) and error.code in ("receiver_required", "association_changed"):
        registered = False
        session.identity = None
        session.identity_ready.clear()
      delay = error.delay if isinstance(error, RetryLater) else min(300.0, 10.0 * 2 ** min(failures - 1, 5))
      warn(str(error) if isinstance(error, RetryLater) else "failed to get AssistNow messages")
      session.cancelled.wait(delay)
