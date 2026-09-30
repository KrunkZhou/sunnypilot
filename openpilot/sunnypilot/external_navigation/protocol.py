"""Bounded navigation relay v1. This module has no device/native dependencies."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import secrets
import struct
import zlib

PORT = 28443
MAX_PACKET = 1200
MAX_SNAPSHOT = 1024
UNKNOWN_AGE = 0xffffffff
HEADER = struct.Struct('<4sBBH16s16s16sI')
ENVELOPE = struct.Struct('<BBHII')
HELLO, CHALLENGE, SNAPSHOT = 1, 2, 3
CHALLENGE_MS = 250


class ProtocolError(ValueError):
  pass


def age_sum(*values: int) -> int:
  return min(UNKNOWN_AGE, sum(values))


def encode(kind: int, relay: bytes, boot: bytes, challenge: bytes, sequence: int, payload: bytes, key: bytes) -> bytes:
  if len(key) != 32 or any(len(x) != 16 for x in (relay, boot, challenge)):
    raise ProtocolError('key_or_identity_length')
  body = HEADER.pack(b'NRLY', 1, kind, len(payload), relay, boot, challenge, sequence) + payload
  if len(body) + 32 > MAX_PACKET:
    raise ProtocolError('oversized_packet')
  return body + hmac.digest(key, body, 'sha256')


def decode(packet: bytes, relay: bytes, key: bytes) -> tuple:
  if not HEADER.size + 32 <= len(packet) <= MAX_PACKET:
    raise ProtocolError('packet_length')
  magic, version, kind, size, identity, boot, challenge, sequence = HEADER.unpack_from(packet)
  if magic != b'NRLY' or version != 1 or kind not in (HELLO, CHALLENGE, SNAPSHOT) or size != len(packet) - HEADER.size - 32:
    raise ProtocolError('packet_header')
  if identity != relay or not hmac.compare_digest(hmac.digest(key, packet[:-32], 'sha256'), packet[-32:]):
    raise ProtocolError('authentication')
  return kind, boot, challenge, sequence, packet[HEADER.size:-32]


@dataclass(frozen=True)
class Navigation:
  available: bool
  token: int
  sequence: int
  session: bytes
  epoch: int
  stream: int
  generation: int
  source_age: int
  fields: dict[int, object] = field(default_factory=dict)

  @property
  def identity(self) -> tuple:
    return self.session, self.epoch, self.stream, self.generation, self.fields.get(16)

  @property
  def supports_hints(self) -> bool:
    return all(tag in self.fields for tag in (2, 3, 16, 17, 18, 19, 20)) and self.fields[17] > 0 and self.fields[19] > 0 and self.fields[18] != UNKNOWN_AGE


def _text(value: bytes, maximum: int) -> str:
  if len(value) > maximum:
    raise ProtocolError('text_length')
  try:
    text = value.decode('utf-8')
  except UnicodeError as exc:
    raise ProtocolError('text_encoding') from exc
  if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in text):
    raise ProtocolError('text_control')
  return text


def _lanes(value: bytes) -> list[dict]:
  if len(value) < 2 or value[0] != 1 or not 1 <= value[1] <= 8:
    raise ProtocolError('lane_header')
  lanes, pos = [], 2
  for _ in range(value[1]):
    if pos + 3 > len(value):
      raise ProtocolError('lane_length')
    status, count, highlighted = value[pos:pos + 3]
    pos += 3
    if status > 2 or not 1 <= count <= 4 or pos + count * 2 > len(value):
      raise ProtocolError('lane_value')
    angles = struct.unpack_from('<' + 'h' * count, value, pos)
    pos += count * 2
    if any(not -180 <= a <= 180 for a in angles) or len(set(angles)) != count:
      raise ProtocolError('lane_angles')
    if (status == 0 and highlighted != 255) or (status != 0 and highlighted >= count):
      raise ProtocolError('lane_highlight')
    lanes.append({'status': status, 'highlighted': highlighted, 'angles': list(angles)})
  if pos != len(value):
    raise ProtocolError('lane_trailing')
  return lanes


def parse_snapshot(raw: bytes, extra_age: int = 0) -> Navigation:
  if not 60 <= len(raw) <= MAX_SNAPSHOT or raw[:3] != b'NV\x01' or raw[3] & ~1:
    raise ProtocolError('snapshot_header')
  if zlib.crc32(raw[:-4]) != struct.unpack_from('<I', raw, len(raw) - 4)[0]:
    raise ProtocolError('snapshot_checksum')
  token, sequence = struct.unpack_from('<II', raw, 4)
  if not token or not sequence:
    raise ProtocolError('snapshot_sequence')
  epoch, stream, generation, source_age = struct.unpack_from('<QQQI', raw, 28)
  fields, seen, pos = {}, set(), 56
  lengths = {1: 1, 2: 1, 3: 4, 4: 4, 5: 8, 7: 1, 9: 1, 16: 8, 17: 4, 18: 4, 19: 4, 20: 4}
  while pos < len(raw) - 4:
    if pos + 3 > len(raw) - 4:
      raise ProtocolError('tlv_header')
    tag, length = struct.unpack_from('<BH', raw, pos)
    pos += 3
    if tag in seen or pos + length > len(raw) - 4:
      raise ProtocolError('tlv_length_or_duplicate')
    seen.add(tag)
    value, pos = raw[pos:pos + length], pos + length
    if tag in lengths:
      if length != lengths[tag]:
        raise ProtocolError('field_length')
      fields[tag] = int.from_bytes(value, 'little')
    elif tag in (6, 8, 10, 11, 12, 13, 14):
      fields[tag] = _text(value, 32 if tag in (6, 8) else 128)
    elif tag == 15:
      fields[tag] = _lanes(value)
  if fields.get(1) not in (*range(7), 255) or any(fields.get(tag, 0) > 4 for tag in (7, 9)):
    raise ProtocolError('route_or_units')
  if not raw[3] and (fields[1] != 255 or stream or generation):
    raise ProtocolError('unavailable_identity')
  if fields[1] != 1 and any(tag in fields for tag in (2, 3, 6, 7, 11, 12, 14, 15, 16, 17, 18, 19, 20)):
    raise ProtocolError('inactive_guidance')
  if 17 in fields and (not fields[17] or any(tag not in fields for tag in (3, 16, 18)) or fields[18] == UNKNOWN_AGE):
    raise ProtocolError('distance_association')
  if 19 in fields and (not fields[19] or 16 not in fields or 20 not in fields or fields[20] == UNKNOWN_AGE):
    raise ProtocolError('maneuver_association')
  for tag in (18, 20):
    if tag in fields:
      fields[tag] = age_sum(fields[tag], extra_age)
  return Navigation(bool(raw[3]), token, sequence, raw[12:28], epoch, stream, generation, age_sum(source_age, extra_age), fields)


class Receiver:
  """One enrolled relay, one outstanding challenge, one latest snapshot."""
  def __init__(self, relay: bytes, key: bytes, random_bytes=secrets.token_bytes):
    if len(relay) != 16 or len(key) != 32:
      raise ProtocolError('configuration')
    self.relay, self.key, self.random_bytes = relay, key, random_bytes
    self.instance = random_bytes(16)
    self.boot = None
    self.peer = None
    self.pending = None
    self.sequence = 0
    self.last_challenge = -1000
    self.received = -10000
    self.navigation = None
    self.reason = 'waiting_for_relay'
    self.state = 0
    self.source_cursor = None
    self.retired_sessions: set[bytes] = set()
    self.last_frame_digest = None
    self.rtt_ms = 0
    self.observed_distance = None
    self.observed_maneuver = None

  def clear(self, reason: str) -> None:
    self.navigation = None
    self.reason = reason

  def challenge(self, now: int) -> bytes | None:
    if self.boot is None or now - self.last_challenge < 200:
      return None
    if self.pending is not None and now - self.pending[2] <= CHALLENGE_MS:
      return None
    self.sequence += 1
    if self.sequence > 0xffffffff:
      self.boot, self.peer, self.pending = None, None, None
      self.clear('sequence_exhausted')
      return None
    nonce = self.random_bytes(16)
    self.pending = nonce, self.sequence, now
    self.last_challenge = now
    return encode(CHALLENGE, self.relay, self.boot, nonce, self.sequence, b'', self.key)

  def receive(self, packet: bytes, peer: tuple, now: int) -> None:
    kind, boot, challenge, sequence, payload = decode(packet, self.relay, self.key)
    if kind == HELLO:
      if sequence or challenge != bytes(16) or payload:
        raise ProtocolError('hello_fields')
      if self.boot != boot or self.peer != peer:
        self.boot, self.peer, self.pending = boot, peer, None
        self.clear('synchronizing')
      return
    if kind != SNAPSHOT or boot != self.boot or peer != self.peer or self.pending is None:
      raise ProtocolError('unexpected_snapshot')
    nonce, expected, sent = self.pending
    if challenge != nonce or sequence != expected or not 0 <= now - sent <= CHALLENGE_MS:
      raise ProtocolError('expired_or_duplicate_challenge')
    self.pending = None
    if len(payload) < ENVELOPE.size:
      raise ProtocolError('envelope_length')
    state, flags, reserved, dwell, heartbeat_age = ENVELOPE.unpack_from(payload)
    if state > 4 or flags & ~1 or reserved or bool(flags) != (len(payload) > ENVELOPE.size):
      raise ProtocolError('envelope_fields')
    heartbeat_expired = age_sum(heartbeat_age, now - sent) >= 5000
    if state != 2 or heartbeat_expired or not flags:
      self.received, self.state, self.rtt_ms = now, state, now - sent
      self.clear('ble_expired' if heartbeat_expired else
                 ('ble_disconnected', 'ble_synchronizing', 'missing_snapshot', 'source_unavailable', 'ble_expired')[state])
      return
    raw = payload[ENVELOPE.size:]
    nav = parse_snapshot(raw, age_sum(dwell, now - sent))
    cursor = nav.session, nav.token, nav.sequence
    if nav.session in self.retired_sessions:
      raise ProtocolError('retired_publisher_session')
    if self.source_cursor is not None and nav.session != self.source_cursor[0]:
      if len(self.retired_sessions) >= 256:
        self.clear('publisher_session_budget_exhausted')
        raise ProtocolError('publisher_session_budget_exhausted')
      self.retired_sessions.add(self.source_cursor[0])
    if self.source_cursor is not None and cursor[0] == self.source_cursor[0]:
      if cursor[1:] < self.source_cursor[1:]:
        raise ProtocolError('source_sequence_regression')
      if cursor == self.source_cursor and self.last_frame_digest != hashlib.sha256(raw).digest():
        raise ProtocolError('source_sequence_conflict')
    for tag, counter, attribute in ((18, 17, 'observed_distance'), (20, 19, 'observed_maneuver')):
      if counter in nav.fields and tag in nav.fields:
        observation = nav.identity, nav.fields[counter]
        previous = getattr(self, attribute)
        if previous is not None and previous[0] == observation:
          nav.fields[tag] = max(nav.fields[tag], age_sum(previous[1], now - previous[2]))
        setattr(self, attribute, (observation, nav.fields[tag], now))
    self.source_cursor, self.last_frame_digest = cursor, hashlib.sha256(raw).digest()
    self.received, self.state, self.rtt_ms = now, state, now - sent
    self.navigation = nav
    self.reason = 'current' if nav.supports_hints else 'guidance_only_legacy_source'

  def tick(self, now: int) -> None:
    if now - self.received > 1000:
      self.clear('relay_stale')
