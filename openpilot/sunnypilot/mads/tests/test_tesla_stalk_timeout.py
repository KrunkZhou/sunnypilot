import unittest
from types import SimpleNamespace

from openpilot.cereal import custom, log
from openpilot.selfdrive.selfdrived.events import ET, EVENTS, Events
from openpilot.selfdrive.selfdrived.state import StateMachine as SelfdriveStateMachine
from openpilot.sunnypilot.mads.state import StateMachine as MadsStateMachine
from openpilot.sunnypilot.selfdrive.selfdrived.events import EventsSP

SelfdriveState = log.SelfdriveState.OpenpilotState
MadsState = custom.ModularAssistiveDrivingSystem.ModularAssistiveDrivingSystemState
EventName = log.OnroadEvent.EventName
EventNameSP = custom.OnroadEventSP.EventName


class TestTeslaStalkTimeout(unittest.TestCase):
  def setUp(self):
    self.selfdrive = SimpleNamespace(
      enabled=False,
      active=False,
      state_machine=SelfdriveStateMachine(),
      events=Events(),
      events_sp=EventsSP(),
    )
    self.mads = MadsStateMachine(SimpleNamespace(selfdrive=self.selfdrive))

  def update(self):
    # Match SelfdriveD.step: normal controls transition before MADS and alert generation.
    self.selfdrive.enabled, self.selfdrive.active = self.selfdrive.state_machine.update(self.selfdrive.events)
    return self.mads.update()

  def assert_cruise_disabled(self):
    self.assertEqual(self.selfdrive.state_machine.state, SelfdriveState.disabled)
    self.assertFalse(self.selfdrive.enabled)
    self.assertFalse(self.selfdrive.active)

  def assert_disabled(self):
    self.assert_cruise_disabled()
    self.assertEqual(self.mads.state, MadsState.disabled)

  def test_timeout_disables_cruise_and_preserves_mads(self):
    for selfdrive_state in (SelfdriveState.disabled, SelfdriveState.enabled, SelfdriveState.overriding):
      for mads_state in (MadsState.enabled, MadsState.overriding, MadsState.paused):
        with self.subTest(selfdrive_state=selfdrive_state, mads_state=mads_state):
          self.selfdrive.state_machine.state = selfdrive_state
          self.mads.state = mads_state
          self.selfdrive.events.clear()
          self.selfdrive.events.add(EventName.teslaRightStalkTimeout)
          if mads_state == MadsState.overriding:
            self.selfdrive.events.add(EventName.steerOverride)

          self.assertEqual(self.update(), (True, mads_state != MadsState.paused))
          self.assert_cruise_disabled()
          self.assertEqual(self.mads.state, mads_state)
          self.assertTrue(self.selfdrive.events.has(EventName.teslaRightStalkTimeout))
          if selfdrive_state != SelfdriveState.disabled:
            self.assertIn(ET.IMMEDIATE_DISABLE, self.selfdrive.state_machine.current_alert_types)

  def test_mads_only_timeout_has_audible_warning(self):
    self.mads.state = MadsState.enabled
    self.selfdrive.events.add(EventName.teslaRightStalkTimeout)

    self.assertEqual(self.update(), (True, True))
    self.assert_cruise_disabled()
    alerts = self.selfdrive.events.create_alerts(self.selfdrive.state_machine.current_alert_types)
    warning = next(alert for alert in alerts if alert.event_type == ET.WARNING)
    self.assertEqual(warning.audible_alert, log.SelfdriveState.AudibleAlert.warningImmediate)
    self.assertEqual(warning.alert_text_1, "Manual Speed Control Required")

  def test_timeout_blocks_simultaneous_enable(self):
    self.selfdrive.events.add(EventName.teslaRightStalkTimeout)
    self.selfdrive.events.add(EventName.pcmEnable)
    self.selfdrive.events_sp.add(EventNameSP.silentLkasEnable)

    self.assertEqual(self.update(), (False, False))
    self.assert_disabled()
    self.assertIn(ET.NO_ENTRY, self.selfdrive.state_machine.current_alert_types)

  def test_message_recovery_alone_does_not_resume_cruise(self):
    self.selfdrive.state_machine.state = SelfdriveState.enabled
    self.mads.state = MadsState.enabled
    self.selfdrive.events.add(EventName.teslaRightStalkTimeout)
    self.update()

    self.selfdrive.events.clear()
    for _ in range(100):
      self.assertEqual(self.update(), (True, True))
      self.assert_cruise_disabled()

    # A new engagement request after recovery may engage under the normal checks.
    self.selfdrive.events.add(EventName.pcmEnable)
    self.assertEqual(self.update(), (True, True))
    self.assertTrue(self.selfdrive.enabled)

  def test_timeout_blocks_cruise_reengagement_while_mads_stays_active(self):
    self.mads.state = MadsState.enabled
    self.selfdrive.events.add(EventName.teslaRightStalkTimeout)
    self.selfdrive.events.add(EventName.pcmEnable)

    self.assertEqual(self.update(), (True, True))
    self.assert_cruise_disabled()
    self.assertIn(ET.NO_ENTRY, self.selfdrive.state_machine.current_alert_types)

  def test_timeout_blocks_paused_mads_resume(self):
    self.mads.state = MadsState.paused
    self.selfdrive.events.add(EventName.teslaRightStalkTimeout)
    self.selfdrive.events_sp.add(EventNameSP.silentLkasEnable)

    self.assertEqual(self.update(), (True, False))
    self.assert_cruise_disabled()
    self.assertEqual(self.mads.state, MadsState.paused)

  def test_timeout_blocks_gear_related_pause_on_enable(self):
    self.selfdrive.events.add(EventName.teslaRightStalkTimeout)
    self.selfdrive.events.add(EventName.wrongGear)
    self.selfdrive.events.add(EventName.pcmEnable)
    self.selfdrive.events_sp.add(EventNameSP.lkasEnable)

    self.assertEqual(self.update(), (False, False))
    self.assert_disabled()

    # A gearbox change and message recovery must not leave MADS armed to resume silently.
    self.selfdrive.events.clear()
    self.selfdrive.events_sp.clear()
    self.assertEqual(self.update(), (False, False))
    self.assert_disabled()

  def test_timeout_preserves_user_requested_pause(self):
    self.mads.state = MadsState.enabled
    self.selfdrive.events.add(EventName.teslaRightStalkTimeout)
    self.selfdrive.events_sp.add(EventNameSP.silentLkasDisable)

    self.assertEqual(self.update(), (True, False))
    self.assert_cruise_disabled()
    self.assertEqual(self.mads.state, MadsState.paused)
    self.assertIn(ET.USER_DISABLE, self.selfdrive.state_machine.current_alert_types)

  def test_other_faults_still_disable_mads_even_with_silent_pause(self):
    for fault in (EventName.accFaulted, EventName.canError, EventName.steerUnavailable):
      for mads_state in (MadsState.enabled, MadsState.overriding, MadsState.paused):
        with self.subTest(fault=fault, mads_state=mads_state):
          self.selfdrive.state_machine.state = SelfdriveState.enabled
          self.mads.state = mads_state
          self.selfdrive.events.clear()
          self.selfdrive.events_sp.clear()
          self.selfdrive.events.add(EventName.teslaRightStalkTimeout)
          self.selfdrive.events.add(fault)
          self.selfdrive.events_sp.add(EventNameSP.silentLkasDisable)

          self.assertEqual(self.update(), (False, False))
          self.assert_disabled()
          self.assertIn(ET.IMMEDIATE_DISABLE, self.selfdrive.state_machine.current_alert_types)

  def test_lateral_safety_mismatch_still_disables_mads(self):
    self.mads.state = MadsState.enabled
    self.selfdrive.events.add(EventName.teslaRightStalkTimeout)
    self.selfdrive.events_sp.add(EventNameSP.controlsMismatchLateral)

    self.assertEqual(self.update(), (False, False))
    self.assert_disabled()

  def test_timeout_preserves_soft_disable(self):
    self.mads.state = MadsState.enabled
    self.selfdrive.events.add(EventName.teslaRightStalkTimeout)
    self.selfdrive.events.add(EventName.overheat)

    self.assertEqual(self.update(), (True, True))
    self.assertEqual(self.mads.state, MadsState.softDisabling)
    for _ in range(301):
      self.update()
    self.assert_disabled()

  def test_silent_pause_without_fault_is_preserved(self):
    self.mads.state = MadsState.enabled
    self.selfdrive.events_sp.add(EventNameSP.silentLkasDisable)

    self.assertEqual(self.update(), (True, False))
    self.assertEqual(self.mads.state, MadsState.paused)
    self.assertIn(ET.USER_DISABLE, self.selfdrive.state_machine.current_alert_types)

  def test_timeout_has_no_entry_and_audible_takeover_alert(self):
    alerts = EVENTS[EventName.teslaRightStalkTimeout]
    self.assertIn(ET.NO_ENTRY, alerts)
    self.assertIn(ET.PERMANENT, alerts)
    immediate = alerts[ET.IMMEDIATE_DISABLE]
    self.assertEqual(immediate.audible_alert, log.SelfdriveState.AudibleAlert.warningImmediate)
    self.assertEqual(immediate.alert_status, log.SelfdriveState.AlertStatus.critical)
    self.assertEqual(immediate.alert_text_1, "TAKE CONTROL IMMEDIATELY")
