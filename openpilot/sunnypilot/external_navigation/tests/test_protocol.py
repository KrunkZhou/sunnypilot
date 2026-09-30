import hashlib
import json
from pathlib import Path
import struct
import unittest
import zlib

from openpilot.sunnypilot.external_navigation.protocol import (
  CHALLENGE, ENVELOPE, HELLO, SNAPSHOT, ProtocolError, Receiver, decode, encode, parse_snapshot,
)

FIXTURES = Path(__file__).with_name('fixtures')
CORPUS = json.loads((FIXTURES / 'corpus.json').read_text())
KEY = bytes.fromhex(CORPUS['key_hex'])
RELAY = bytes.fromhex(CORPUS['relay_id_hex'])
BOOT = bytes.fromhex(CORPUS['boot_hex'])
NONCE = bytes.fromhex(CORPUS['challenge_hex'])
PEER = ('192.168.43.2', 28443)


def fixture(name):
  return (FIXTURES / name).read_bytes()


def fields(raw, changes=None, remove=()):
  result, pos = bytearray(raw[:56]), 56
  changes = changes or {}
  while pos < len(raw) - 4:
    tag, size = struct.unpack_from('<BH', raw, pos)
    value = raw[pos + 3:pos + 3 + size]
    pos += size + 3
    if tag in remove:
      continue
    value = changes.get(tag, value)
    result.extend(struct.pack('<BH', tag, len(value)) + value)
  return bytes(result) + struct.pack('<I', zlib.crc32(result))


class ProtocolTests(unittest.TestCase):
  def receiver(self, now=0):
    receiver = Receiver(RELAY, KEY, lambda n: NONCE)
    receiver.receive(fixture('hello.bin'), PEER, now)
    receiver.sequence = 6
    self.assertEqual(receiver.challenge(now), fixture('challenge.bin'))
    return receiver

  def test_shared_fixture_hashes(self):
    for name, digest in CORPUS['sha256'].items():
      self.assertEqual(hashlib.sha256(fixture(name)).hexdigest(), digest, name)

  def test_shared_golden_wire(self):
    self.assertEqual(decode(fixture('hello.bin'), RELAY, KEY)[0], HELLO)
    self.assertEqual(decode(fixture('challenge.bin'), RELAY, KEY)[0], CHALLENGE)
    receiver = self.receiver()
    receiver.receive(fixture('snapshot.bin'), PEER, 50)
    nav = receiver.navigation
    self.assertTrue(nav.supports_hints)
    self.assertEqual(nav.fields[16], 42)
    self.assertEqual(nav.fields[3], 0)  # A supplied zero must stay zero.
    self.assertEqual(nav.fields[18], 123 + 20 + 50)
    self.assertEqual(nav.fields[20], 456 + 20 + 50)
    self.assertEqual(nav.fields[12], 'Turn left')

  def test_malformed_corpus(self):
    for name in ('bad-hmac.bin', 'bad-reserved.bin', 'bad-crc.bin', 'oversized.bin', 'truncated.bin'):
      with self.subTest(name=name):
        receiver = self.receiver()
        with self.assertRaises(ProtocolError):
          receiver.receive(fixture(name), PEER, 50)
        self.assertIsNone(receiver.navigation)

  def test_legacy_guidance_only(self):
    receiver = self.receiver()
    receiver.receive(fixture('legacy-snapshot.bin'), PEER, 50)
    self.assertFalse(receiver.navigation.supports_hints)

  def test_partial_freshness_without_distance(self):
    raw = fields(fixture('extended-navigation.bin'), {18: b'\xff' * 4}, remove=(3, 17))
    self.assertFalse(parse_snapshot(raw).supports_hints)
    with self.assertRaises(ProtocolError):
      parse_snapshot(fields(raw, remove=(16,)))

  def test_duplicates_and_wrong_peer(self):
    receiver = self.receiver()
    with self.assertRaises(ProtocolError):
      receiver.receive(fixture('snapshot.bin'), ('192.168.43.3', 28443), 20)
    receiver.receive(fixture('snapshot.bin'), PEER, 50)
    with self.assertRaises(ProtocolError):
      receiver.receive(fixture('snapshot.bin'), PEER, 51)
    self.assertEqual(receiver.received, 50)

  def test_expired_challenge_and_authentication(self):
    receiver = self.receiver()
    with self.assertRaises(ProtocolError):
      receiver.receive(fixture('snapshot.bin'), PEER, 251)
    with self.assertRaises(ProtocolError):
      decode(fixture('snapshot.bin'), bytes(16), KEY)
    with self.assertRaises(ProtocolError):
      decode(fixture('snapshot.bin'), RELAY, bytes(32))

  def test_challenge_pacing(self):
    receiver = self.receiver()
    self.assertIsNone(receiver.challenge(200))
    self.assertIsNotNone(receiver.challenge(251))
    self.assertIsNone(receiver.challenge(300))

  def test_heartbeat_does_not_preserve_guidance(self):
    receiver = self.receiver()
    receiver.receive(fixture('snapshot.bin'), PEER, 50)
    receiver.receive(fixture('hello.bin'), PEER, 900)
    receiver.tick(1051)
    self.assertIsNone(receiver.navigation)

  def test_bad_snapshot_never_renews_guidance(self):
    for bad_raw in (fixture('extended-navigation.bin')[:-1] + b'\xff',
                    fields(fixture('extended-navigation.bin'), {17: bytes(4)})):
      receiver = self.receiver()
      receiver.receive(fixture('snapshot.bin'), PEER, 50)
      challenge = receiver.challenge(900)
      _, boot, nonce, sequence, _ = decode(challenge, RELAY, KEY)
      packet = encode(SNAPSHOT, RELAY, boot, nonce, sequence, ENVELOPE.pack(2, 1, 0, 20, 30) + bad_raw, KEY)
      with self.assertRaises(ProtocolError):
        receiver.receive(packet, PEER, 950)
      self.assertEqual(receiver.received, 50)
      receiver.tick(1051)
      self.assertIsNone(receiver.navigation)

  def test_reset_and_new_boot_clear(self):
    receiver = self.receiver()
    receiver.receive(fixture('snapshot.bin'), PEER, 50)
    receiver.challenge(500)
    nonce, seq, _ = receiver.pending
    packet = encode(SNAPSHOT, RELAY, BOOT, nonce, seq, ENVELOPE.pack(0, 0, 0, 0, 0), KEY)
    receiver.receive(packet, PEER, 510)
    self.assertIsNone(receiver.navigation)
    receiver.receive(encode(HELLO, RELAY, b'x' * 16, bytes(16), 0, b'', KEY), PEER, 520)
    self.assertIsNone(receiver.pending)
    with self.assertRaises(ProtocolError):
      receiver.receive(fixture('snapshot.bin'), PEER, 530)

  def test_unknown_tlv_duplicate_and_utf8(self):
    raw = fixture('extended-navigation.bin')
    body = raw[:-4] + b'\xc8\x02\x00ok'
    raw_unknown = body + struct.pack('<I', zlib.crc32(body))
    self.assertTrue(parse_snapshot(raw_unknown).supports_hints)
    body = raw[:-4] + b'\x01\x01\x00\x01'
    with self.assertRaises(ProtocolError):
      parse_snapshot(body + struct.pack('<I', zlib.crc32(body)))
    for value in (b'\xff', b'\x00', b'x' * 129):
      with self.assertRaises(ProtocolError):
        parse_snapshot(fields(raw, {12: value}))


class FreshnessRegressionTests(unittest.TestCase):
  receiver = ProtocolTests.receiver
  def send_again(self, receiver, raw, now=900, dwell=0):
    challenge = receiver.challenge(now)
    _, boot, nonce, sequence, _ = decode(challenge, RELAY, KEY)
    packet = encode(SNAPSHOT, RELAY, boot, nonce, sequence, ENVELOPE.pack(2, 1, 0, dwell, 0) + raw, KEY)
    receiver.receive(packet, PEER, now + 20)

  def test_repeat_cannot_reduce_observation_age(self):
    receiver = self.receiver()
    receiver.receive(fixture('snapshot.bin'), PEER, 50)
    old_age = receiver.navigation.fields[18]
    self.send_again(receiver, fixture('extended-navigation.bin'))
    self.assertGreaterEqual(receiver.navigation.fields[18], old_age + 870)

  def test_regression_does_not_refresh_lease(self):
    receiver = self.receiver()
    receiver.receive(fixture('snapshot.bin'), PEER, 50)
    raw = bytearray(fixture('extended-navigation.bin'))
    sequence = struct.unpack_from('<I', raw, 8)[0]
    struct.pack_into('<I', raw, 8, max(0, sequence - 1))
    struct.pack_into('<I', raw, len(raw) - 4, zlib.crc32(raw[:-4]))
    with self.assertRaises(ProtocolError):
      self.send_again(receiver, bytes(raw))
    self.assertEqual(receiver.received, 50)

  def test_heartbeat_lease_includes_challenge_round_trip(self):
    for heartbeat, accepted in ((4799, True), (4800, False), (4900, False), (0xffffffff, False)):
      receiver = self.receiver()
      packet = encode(SNAPSHOT, RELAY, BOOT, NONCE, 7,
                      ENVELOPE.pack(2, 1, 0, 20, heartbeat) + fixture('extended-navigation.bin'), KEY)
      receiver.receive(packet, PEER, 200)
      self.assertEqual(receiver.navigation is not None, accepted)
      if not accepted:
        self.assertEqual(receiver.reason, 'ble_expired')

  def test_socket_round_trip(self):
    import socket
    receiver = Receiver(RELAY, KEY, lambda n: NONCE)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
      server.bind(('127.0.0.1', 0))
      server.settimeout(1)
      client.settimeout(1)
      client.sendto(fixture('hello.bin'), server.getsockname())
      packet, peer = server.recvfrom(1201)
      receiver.receive(packet, peer, 0)
      server.sendto(receiver.challenge(0), peer)
      challenge, _ = client.recvfrom(1201)
      _, boot, nonce, sequence, _ = decode(challenge, RELAY, KEY)
      response = encode(SNAPSHOT, RELAY, boot, nonce, sequence,
                        ENVELOPE.pack(2, 1, 0, 20, 30) + fixture('extended-navigation.bin'), KEY)
      client.sendto(response, server.getsockname())
      packet, peer = server.recvfrom(1201)
      receiver.receive(packet, peer, 10)
      self.assertTrue(receiver.navigation.supports_hints)


if __name__ == '__main__':
  unittest.main()
