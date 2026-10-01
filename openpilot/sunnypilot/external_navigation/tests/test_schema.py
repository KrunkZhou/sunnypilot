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
  def test_navigation_status_diagnostics_after_long_uptime(self):
    import capnp
    from openpilot.sunnypilot.external_navigation.tests.test_speed_adapter import NavigationSpeedAdapterTests
    root = Path(__file__).resolve().parents[4]
    schema = capnp.load(str(root / 'openpilot/cereal/log.capnp'), imports=[str(root / 'opendbc_repo/opendbc/car')])
    helper = NavigationSpeedAdapterTests()
    for now, receipt in ((50 * 86400 * 1_000_000_000, 0), (10_000_000_000, 0), (10_000_000_000, 11_000_000_000)):
      adapter, sm = helper.setup_adapter()
      sm['externalNavigationSP'].receiveMonoTime = receipt
      sm['externalNavigationSP'].connected = False
      with patch('openpilot.sunnypilot.external_navigation.speed_adapter.read_mode', return_value=0), \
           patch('openpilot.sunnypilot.external_navigation.speed_adapter.read_turn_speed_control', return_value=False):
        self.assertIsNone(adapter.update(sm, baseline_target=25., now_ns=now).speed_cap)
      message = schema.Event.new_message()
      out = message.init('longitudinalPlanSP').navigationSpeedControl
      adapter.fill(out, baseline_source='cruise', baseline_target=25.)
      with schema.Event.from_bytes(message.to_bytes()) as decoded:
        diag = decoded.longitudinalPlanSP.navigationSpeedControl
        self.assertFalse(diag.speedSelected)
        self.assertLessEqual(diag.deliveryAgeMs, 0xffffffff)

  def test_navigation_speed_diagnostics_roundtrip(self):
    import capnp
    from openpilot.sunnypilot.external_navigation.speed_adapter import ExternalNavigationSpeed
    from openpilot.sunnypilot.external_navigation.speed_control import NavigationSpeedSample
    root = Path(__file__).resolve().parents[4]
    schema = capnp.load(str(root / 'openpilot/cereal/log.capnp'), imports=[str(root / 'opendbc_repo/opendbc/car')])
    adapter = ExternalNavigationSpeed(SimpleNamespace(openpilotLongitudinalControl=True))
    sample = NavigationSpeedSample((b'p' * 16, 1, 2, 3, 4), (b'r' * 16, 5), 1, 2, 100,
                                   6, 7, 800, 4000, 201, receive_mono_time=9_799_000_000,
                                   source_age_ms=800, transport_rtt_ms=91, snapshot_sequence=123)
    adapter.enabled = True
    adapter.decision = adapter.controller.update(sample, now_ns=10_000_000_000, v_ego=20., baseline_target=25.)
    for cruise_selected in (False, True):
      message = schema.Event.new_message()
      plan = message.init('longitudinalPlanSP')
      plan.longitudinalPlanSource = 'externalNavigation'
      adapter.fill(plan.navigationSpeedControl, baseline_source='sccVision', baseline_target=25., cruise_selected=cruise_selected)
      with schema.Event.from_bytes(message.to_bytes()) as decoded:
        result = decoded.longitudinalPlanSP
        self.assertEqual(result.longitudinalPlanSource, 'externalNavigation')
        diag = result.navigationSpeedControl
        self.assertTrue(diag.capAvailable)
        self.assertTrue(diag.speedSelected)
        self.assertEqual(diag.cruiseCandidateSelected, cruise_selected)
        self.assertEqual(diag.baselineSource, 'sccVision')
        self.assertEqual(diag.publisherSession, b'p' * 16)
        self.assertEqual(diag.receiverSession, b'r' * 16)
        self.assertEqual(diag.maneuverId, 4)
        self.assertEqual(diag.distanceAgeMs, 800)
        self.assertEqual(diag.maneuverAgeMs, 4000)
        self.assertEqual(diag.deliveryAgeMs, 201)
        self.assertEqual(diag.receiveMonoTime, 9_799_000_000)
        self.assertEqual(diag.snapshotSequence, 123)
        self.assertAlmostEqual(diag.appliedCap, adapter.decision.speed_cap, places=5)
    legacy = schema.Event.new_message().init('longitudinalPlanSP')
    self.assertFalse(legacy.navigationSpeedControl.capAvailable)
    self.assertFalse(legacy.navigationSpeedControl.speedSelected)

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
