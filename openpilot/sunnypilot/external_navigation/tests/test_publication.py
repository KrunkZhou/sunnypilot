import unittest

from openpilot.sunnypilot.external_navigation.protocol import ENVELOPE, SNAPSHOT, ProtocolError, Receiver, encode
from openpilot.sunnypilot.external_navigation.receiver import PublicationSchedule
from openpilot.sunnypilot.external_navigation.tests.test_protocol import BOOT, KEY, NONCE, PEER, RELAY, fixture


class PublicationTests(unittest.TestCase):
  def receiver(self):
    receiver = Receiver(RELAY, KEY, lambda n: NONCE)
    schedule = PublicationSchedule()
    self.assertTrue(schedule.due(0.))  # Initial status publication.
    schedule.receive(receiver, fixture('hello.bin'), PEER, 0)
    receiver.sequence = 6
    receiver.challenge(0)
    return receiver, schedule

  def test_accepted_snapshot_publishes_without_waiting_or_changing_age(self):
    receiver, schedule = self.receiver()
    schedule.receive(receiver, fixture('snapshot.bin'), PEER, 50)
    age = receiver.navigation.fields[18]
    self.assertTrue(schedule.due(.05))
    self.assertFalse(schedule.due(.06))
    self.assertFalse(schedule.due(.249))
    self.assertTrue(schedule.due(.25))
    self.assertEqual(receiver.received, 50)
    self.assertEqual(receiver.navigation.fields[18], age)

  def test_hello_and_invalid_packets_cannot_force_publication(self):
    for name in ('bad-hmac.bin', 'bad-reserved.bin', 'bad-crc.bin', 'truncated.bin'):
      receiver, schedule = self.receiver()
      schedule.receive(receiver, fixture('hello.bin'), PEER, 40)
      self.assertFalse(schedule.due(.04))
      with self.assertRaises(ProtocolError):
        schedule.receive(receiver, fixture(name), PEER, 50)
      self.assertFalse(schedule.due(.05))
      self.assertTrue(schedule.due(.2))  # Idle/error status still reaches subscribers.

  def test_accepted_then_duplicate_batch_publishes_once(self):
    receiver, schedule = self.receiver()
    schedule.receive(receiver, fixture('snapshot.bin'), PEER, 50)
    with self.assertRaises(ProtocolError):
      schedule.receive(receiver, fixture('snapshot.bin'), PEER, 50)
    self.assertTrue(schedule.due(.05))
    self.assertFalse(schedule.due(.05))
    with self.assertRaises(ProtocolError):
      schedule.receive(receiver, fixture('snapshot.bin'), PEER, 100)
    self.assertFalse(schedule.due(.1))

  def test_valid_reset_is_immediate_and_removes_guidance(self):
    receiver, schedule = self.receiver()
    schedule.receive(receiver, fixture('snapshot.bin'), PEER, 50)
    schedule.due(.05)
    receiver.challenge(200)
    nonce, sequence, _ = receiver.pending
    packet = encode(SNAPSHOT, RELAY, BOOT, nonce, sequence, ENVELOPE.pack(0, 0, 0, 0, 0), KEY)
    schedule.receive(receiver, packet, PEER, 210)
    self.assertTrue(schedule.due(.21))
    self.assertIsNone(receiver.navigation)
    self.assertEqual(receiver.reason, 'ble_disconnected')
    self.assertFalse(schedule.due(.22))

  def test_expired_source_still_publishes_periodic_status(self):
    receiver, schedule = self.receiver()
    schedule.receive(receiver, fixture('snapshot.bin'), PEER, 50)
    schedule.due(.05)
    receiver.tick(1051)
    self.assertTrue(schedule.due(1.051))
    self.assertIsNone(receiver.navigation)
    self.assertEqual(receiver.reason, 'relay_stale')


if __name__ == '__main__':
  unittest.main()
