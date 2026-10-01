import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openpilot.sunnypilot.external_navigation.receiver import publish
from openpilot.sunnypilot.external_navigation.tests.test_protocol import fixture, KEY, NONCE, PEER, RELAY
from openpilot.sunnypilot.external_navigation.protocol import Receiver


@unittest.skipUnless(importlib.util.find_spec('capnp'), 'pycapnp is required for schema serialization')
class SchemaTests(unittest.TestCase):
  def test_latched_turn_event_and_input_age_roundtrip(self):
    import capnp
    root = Path(__file__).resolve().parents[4]
    schema = capnp.load(str(root / 'openpilot/cereal/log.capnp'), imports=[str(root / 'opendbc_repo/opendbc/car')])
    message = schema.Event.new_message()
    out = message.init('modelDataV2SP')
    out.navigationHint = 0  # Later frame after the original pulse.
    out.navigationTurnEvent = 2
    out.navigationTurnEventMonoTime = 10_000_000_000
    out.navigationInputDistanceAgeMs = 1001
    out.navigationInputReceiveAgeMs = 201
    out.navigationInputMonoTime = 10_201_000_000
    out.navigationDistanceObservation = 123
    out.navigationTransportToken = 98
    with schema.Event.from_bytes(message.to_bytes()) as decoded:
      hint = decoded.modelDataV2SP
      self.assertEqual(hint.navigationHint, 0)
      self.assertEqual(hint.navigationTurnEvent, 2)
      self.assertEqual(hint.navigationTurnEventMonoTime, 10_000_000_000)
      self.assertEqual(hint.navigationInputDistanceAgeMs, 1001)
      self.assertEqual(hint.navigationInputReceiveAgeMs, 201)
      self.assertEqual(hint.navigationInputMonoTime, 10_201_000_000)
      self.assertEqual(hint.navigationDistanceObservation, 123)
      self.assertEqual(hint.navigationTransportToken, 98)
    empty = schema.Event.new_message()
    old = empty.init('modelDataV2SP')
    old.navigationAssisted = True  # Legacy eligibility fields cannot fabricate a new event.
    self.assertEqual(old.navigationTurnEvent, 0)
    self.assertEqual(old.navigationTurnEventMonoTime, 0)

  def test_registered_reserved_slot_and_publication(self):
    import capnp
    root = Path(__file__).resolve().parents[4]
    schema = capnp.load(str(root / 'openpilot/cereal/log.capnp'), imports=[str(root / 'opendbc_repo/opendbc/car')])
    field = schema.Event.schema.fields['externalNavigationSP'].proto
    self.assertEqual(field.ordinal.explicit, 136)
    self.assertEqual(schema.Event.schema.fields['externalNavigationSP'].schema.node.id, 0xcb9fd56c7057593a)
    def new_message(name):
      message = schema.Event.new_message()
      message.init(name)
      return message
    output = []
    pm = SimpleNamespace(send=lambda name, message: output.append(message.to_bytes()))
    receiver = Receiver(RELAY, KEY, lambda n: NONCE)
    receiver.receive(fixture('hello.bin'), PEER, 0)
    receiver.sequence = 6
    receiver.challenge(0)
    receiver.receive(fixture('snapshot.bin'), PEER, 50)
    fake_cereal = SimpleNamespace(messaging=SimpleNamespace(new_message=new_message))
    with patch.dict('sys.modules', {'openpilot.cereal': fake_cereal}):
      publish(pm, receiver, 100, receiver.reason)
      publish(pm, None, 200, 'off')
    with schema.Event.from_bytes(output[0]) as message:
      nav = message.externalNavigationSP
      self.assertTrue(nav.connected)
      self.assertTrue(nav.sourceFresh)
      self.assertEqual(nav.receiveMonoTime, 50_000_000)
      self.assertEqual(nav.distanceAgeMs, 123 + 20 + 50)
      self.assertEqual(nav.nextDistance, 0)
      self.assertEqual(nav.maneuverId, 42)
    with schema.Event.from_bytes(output[1]) as message:
      self.assertFalse(message.externalNavigationSP.connected)
      self.assertFalse(message.externalNavigationSP.available)


if __name__ == '__main__':
  unittest.main()
