"""Independent reconnect, freshness and driver-priority regressions."""
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from openpilot.sunnypilot.external_navigation.model_adapter import ExternalNavigationHints
from openpilot.sunnypilot.external_navigation.policy import TurnPolicy
from openpilot.sunnypilot.external_navigation.protocol import ENVELOPE, HELLO, SNAPSHOT, ProtocolError, Receiver, encode

FIXTURE = Path(__file__).with_name('fixtures') / 'extended-navigation.bin'
KEY, RELAY, BOOT = bytes(range(32)), bytes(range(16)), bytes(range(16, 32))
PEER = ('192.168.43.2', 28443)


def snapshot(*, token=1, sequence=1, session=b'a' * 16, observation=1, age=0, distance=30):
  """Re-encode the shared actual Android fixture with explicit source observations."""
  raw = FIXTURE.read_bytes()
  result = bytearray(raw[:56])
  struct.pack_into('<II', result, 4, token, sequence)
  result[12:28] = session
  changes = {3: struct.pack('<I', distance), 17: struct.pack('<I', observation), 18: struct.pack('<I', age)}
  pos = 56
  while pos < len(raw) - 4:
    tag, length = struct.unpack_from('<BH', raw, pos)
    value = changes.get(tag, raw[pos + 3:pos + 3 + length])
    result.extend(struct.pack('<BH', tag, len(value)) + value)
    pos += length + 3
  return bytes(result) + struct.pack('<I', zlib.crc32(result))


class ReceiverSafetyTests(unittest.TestCase):
  def setUp(self):
    self.receiver = Receiver(RELAY, KEY)
    self.receiver.receive(encode(HELLO, RELAY, BOOT, bytes(16), 0, b'', KEY), PEER, 0)

  def send(self, now, raw=None, state=2):
    self.assertIsNotNone(self.receiver.challenge(now))
    nonce, sequence, _ = self.receiver.pending
    payload = ENVELOPE.pack(state, int(raw is not None), 0, 0, 0) + (raw or b'')
    packet = encode(SNAPSHOT, RELAY, BOOT, nonce, sequence, payload, KEY)
    self.receiver.receive(packet, PEER, now + 10)

  def test_ble_reconnect_accepts_new_token_without_forgetting_source_age(self):
    self.send(0, snapshot(token=3, sequence=25, age=50))
    self.send(200, state=0)
    self.assertIsNone(self.receiver.navigation)
    self.send(400, snapshot(token=4, sequence=1, age=0))
    self.assertEqual(self.receiver.navigation.token, 4)
    self.assertEqual(self.receiver.navigation.session, b'a' * 16)
    self.assertGreaterEqual(self.receiver.navigation.fields[18], 460)
    with self.assertRaisesRegex(ProtocolError, 'source_sequence_regression'):
      self.send(600, snapshot(token=3, sequence=26, age=0))
    self.assertEqual(self.receiver.received, 410)
    self.receiver.tick(1411)
    self.assertIsNone(self.receiver.navigation)

  def test_new_snapshot_sequence_and_heartbeat_do_not_refresh_same_distance(self):
    self.send(0, snapshot(age=900))
    self.receiver.receive(encode(HELLO, RELAY, BOOT, bytes(16), 0, b'', KEY), PEER, 190)
    self.send(200, snapshot(sequence=2, age=0))
    self.assertGreaterEqual(self.receiver.navigation.fields[18], 1110)
    self.send(400, snapshot(sequence=3, observation=2, age=0))
    self.assertEqual(self.receiver.navigation.fields[18], 10)

  def test_replaced_publisher_cannot_return_or_renew_the_lease(self):
    self.send(0, snapshot())
    self.send(200, snapshot(session=b'b' * 16))
    with self.assertRaisesRegex(ProtocolError, 'retired_publisher_session'):
      self.send(400, snapshot(session=b'a' * 16, sequence=2))
    self.assertEqual(self.receiver.received, 210)
    self.assertEqual(self.receiver.navigation.session, b'b' * 16)

  def test_identical_sequence_with_different_observation_is_not_fresh_data(self):
    self.send(0, snapshot(age=200))
    with self.assertRaisesRegex(ProtocolError, 'source_sequence_conflict'):
      self.send(200, snapshot(age=0, observation=2))
    self.assertEqual(self.receiver.received, 10)


class Subscriber(dict):
  def __init__(self, **values):
    super().__init__(values)
    self.valid = dict.fromkeys(values, True)
    self.seen = dict.fromkeys(values, True)
    self.alive = dict.fromkeys(values, True)

  def update(self, timeout):
    pass


class AdapterSafetyTests(unittest.TestCase):
  def setUp(self):
    self.clock = SimpleNamespace(seconds=10., mode=1)
    for target, effect in (
      ('openpilot.sunnypilot.external_navigation.model_adapter.time.monotonic', lambda: self.clock.seconds),
      ('openpilot.sunnypilot.external_navigation.model_adapter.time.monotonic_ns', lambda: int(self.clock.seconds * 1e9)),
      ('openpilot.sunnypilot.external_navigation.model_adapter.read_mode', lambda: self.clock.mode),
    ):
      mock = patch(target, side_effect=effect)
      mock.start()
      self.addCleanup(mock.stop)
    self.make_adapter()

  def make_adapter(self):
    self.nav = SimpleNamespace(
      receiveMonoTime=10_000_000_000, receiverSession=b'r' * 16, publisherSession=b'p' * 16,
      cacheEpoch=1, streamId=2, generation=3, token=4, maneuverId=42, routeState=1, maneuverType=2,
      hasNextDistance=True, nextDistance=30, distanceObservation=1, distanceAgeMs=0,
      connected=True, available=True, hasManeuverId=True, maneuverObservation=1,
    )
    self.hints = ExternalNavigationHints.__new__(ExternalNavigationHints)
    self.hints.sm = Subscriber(externalNavigationSP=self.nav)
    self.hints.policy = TurnPolicy()
    self.hints.mode, self.hints.next_parameter_read = 0, 0
    self.hints.last_model = self.hints.last_transport = None
    self.car = SimpleNamespace(steeringPressed=False, brakePressed=False, leftBlinker=False, rightBlinker=False,
                               leftBlindspot=False, rightBlindspot=False, vEgo=4.)
    self.sm = Subscriber(carState=self.car, carControl=SimpleNamespace(latActive=True))
    self.dh = SimpleNamespace(lane_change_state=0, desire=0, lane_turn_controller=SimpleNamespace(lane_turn_value=8.))
    self.model = object()

  def update(self, distance=None):
    if distance is not None:
      self.nav.nextDistance = distance
      self.nav.distanceObservation += 1
    desire = self.hints.update(self.sm, self.dh, self.model)
    self.assertEqual(desire, self.dh.desire or self.hints.decision.desire)
    return self.hints.decision

  def test_new_ble_token_or_receiver_restart_discards_armed_state(self):
    for field, value in (('token', 5), ('receiverSession', b's' * 16)):
      with self.subTest(field=field):
        self.make_adapter()
        self.update()
        self.assertTrue(self.hints.policy.armed)
        setattr(self.nav, field, value)  # No intermediate unavailable publication was observed.
        self.assertEqual(self.update(14).proposal, 0)
        self.assertFalse(self.hints.policy.armed)

  def test_consumed_maneuver_survives_ble_and_receiver_restart(self):
    self.update()
    self.assertEqual(self.update(14).proposal, 2)
    consumed = set(self.hints.policy.consumed)
    for field, value in (('token', 5), ('receiverSession', b's' * 16)):
      setattr(self.nav, field, value)
      self.assertEqual(self.update(30).proposal, 0)
      self.assertEqual(self.update(14).proposal, 0)
      self.assertEqual(self.hints.policy.consumed, consumed)

  def test_off_on_inside_boundary_does_not_trigger_late_pulse(self):
    self.update()
    self.clock.mode = 0
    self.clock.seconds = 11.1
    self.nav.receiveMonoTime = int(self.clock.seconds * 1e9)
    self.assertEqual(self.update().reason, 'off')
    self.clock.mode = 1
    self.clock.seconds = 12.2
    self.nav.receiveMonoTime = int(self.clock.seconds * 1e9)
    self.assertEqual(self.update(14).proposal, 0)

  def test_reset_reroute_and_stale_distance_discard_approach(self):
    for field, value in (('routeState', 3), ('connected', False), ('available', False), ('distanceAgeMs', 1001)):
      with self.subTest(field=field):
        self.make_adapter()
        self.update()
        original = getattr(self.nav, field)
        setattr(self.nav, field, value)
        self.assertEqual(self.update().proposal, 0)
        setattr(self.nav, field, original)
        self.assertEqual(self.update(14).proposal, 0)
    for field in ('cacheEpoch', 'streamId', 'generation', 'maneuverId'):
      with self.subTest(identity=field):
        self.make_adapter()
        self.update()
        setattr(self.nav, field, getattr(self.nav, field) + 1)
        self.assertEqual(self.update(14).proposal, 0)

  def test_driver_inputs_and_existing_desires_have_precedence(self):
    for field in ('steeringPressed', 'brakePressed', 'leftBlinker', 'rightBlinker', 'leftBlindspot', 'rightBlindspot'):
      with self.subTest(driver=field):
        self.make_adapter()
        self.update()
        setattr(self.car, field, True)
        self.assertEqual(self.update(14).proposal, 0)
        setattr(self.car, field, False)
        self.assertEqual(self.update(10).proposal, 0)
    for field, value in (('lane_change_state', 1), ('desire', 3), ('desire', 4), ('desire', 1)):
      with self.subTest(existing=field, value=value):
        self.make_adapter()
        self.update()
        setattr(self.dh, field, value)
        self.assertEqual(self.update(14).proposal, 0)

  def test_observation_must_change_and_branch_hints_remain_advisory(self):
    self.update()
    self.nav.nextDistance = 14
    self.assertEqual(self.update().proposal, 0)
    self.nav.distanceObservation += 1
    self.assertEqual(self.update().proposal, 2)
    self.assertEqual(self.update().proposal, 0)
    for maneuver_type in (14, 23, 53):
      with self.subTest(branch=maneuver_type):
        self.make_adapter()
        self.nav.maneuverType = maneuver_type
        self.assertEqual(self.update().reason, 'branch_right_advisory_only')
        self.assertEqual(self.update(14).proposal, 0)


if __name__ == '__main__':
  unittest.main()
