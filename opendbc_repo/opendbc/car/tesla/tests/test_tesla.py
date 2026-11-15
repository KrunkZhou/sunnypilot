import re
import unittest

from opendbc.can import CANPacker
from opendbc.car import Bus, gen_empty_fingerprint
from opendbc.car.structs import CarParams, CarParamsSP
from opendbc.car.tesla.carstate import CarState
from opendbc.car.tesla.interface import CarInterface
from opendbc.car.tesla.fingerprints import FW_VERSIONS
from opendbc.car.tesla.radar_interface import RADAR_START_ADDR
from opendbc.car.tesla.values import CANBUS, CAR, DBC

Ecu = CarParams.Ecu

# Fields prefixed unknown_* we observe structurally but don't know the meaning of.
# Only `platform` has evidence-backed semantic meaning (matches car_model in FW_VERSIONS).
#
# unknown_prefix is everything before the comma; we don't split it because we don't know what its
# parts mean, but observed shape is: <family>_<package>_<triplet> (<build>), e.g.
#   TeMYG4 _ Main     _ 0.0.0 (78)     or     TeM3 _ SP_XP002p2 _ 0.0.0 (23)
#   family   package    triplet build           family  package    triplet build
#
# After the comma, the version string decomposes into:
#   platform             : E/Y/X = car model (Model 3 / Y / X). The only field with known meaning.
#   variant_code         : differentiator WITHIN a platform — hardware/trim/calibration bits packed
#                          into <digit?><letters?><3-digit series>, e.g. '4HP015', '4003', 'L014',
#                          'PR003'. We don't fully know what the parts mean individually, but the
#                          whole string identifies a specific variant within the car model.
#   software_major/minor : numeric components after the first '.' — conventional release numbers.
#                          minor is optional (e.g. 'E4S014.27' has no minor).
#
# Suspected (not confirmed): for M3/MY, `TeM3_*` outer + no-leading-digit variant_code == HW3, and
# `TeMYG4_*` outer + leading-'4' variant_code == HW4 (the 'G4' in TeMYG4 likely denotes Gen 4).
#
# Example full parse of 'TeMYG4_Main_0.0.0 (78),E4HP015.05.0':
#   unknown_prefix='TeMYG4_Main_0.0.0 (78)'
#   platform=E  variant_code=4HP015  software_major=05  software_minor=0
FW_RE = re.compile(
  rb'^(?P<unknown_prefix>.+),' +
  rb'(?P<platform>[EYX])' +
  rb'(?P<variant_code>\d?[A-Z]*\d{3})' +
  rb'\.(?P<software_major>\d+)' +
  rb'(?:\.(?P<software_minor>\d+))?$'
)

PLATFORM_TO_CAR = {
  b'E': CAR.TESLA_MODEL_3,
  b'Y': CAR.TESLA_MODEL_Y,
  b'X': CAR.TESLA_MODEL_X,
}


class TestTeslaFingerprint(unittest.TestCase):
  def test_fw_platform_code(self):
    # Every EPS FW must parse and its platform letter must match the car it's filed under.
    for car_model, ecus in FW_VERSIONS.items():
      for fw in ecus.get((Ecu.eps, 0x730, None), []):
        m = FW_RE.match(fw)

        assert m is not None, f"Unparsable FW: {fw}"
        assert PLATFORM_TO_CAR[m['platform']] == car_model, f"Platform letter {m['platform']!r} != {car_model.value}: {fw}"

  def test_radar_detection(self):
    # Test radar availability detection for cars with radar DBC defined
    for radar in (True, False):
      fingerprint = gen_empty_fingerprint()
      if radar:
        fingerprint[1][RADAR_START_ADDR] = 8
      CP = CarInterface.get_params(CAR.TESLA_MODEL_3, fingerprint, [], False, False, False)
      assert CP.radarUnavailable != radar

  def test_no_radar_car(self):
    # Model X doesn't have radar DBC defined, should always be unavailable
    for radar in (True, False):
      fingerprint = gen_empty_fingerprint()
      if radar:
        fingerprint[1][RADAR_START_ADDR] = 8
      CP = CarInterface.get_params(CAR.TESLA_MODEL_X, fingerprint, [], False, False, False)
      assert CP.radarUnavailable  # Always unavailable since no radar DBC


class TestTeslaCarState(unittest.TestCase):
  def setUp(self):
    CP = CarInterface.get_params(CAR.TESLA_MODEL_3, gen_empty_fingerprint(), [], False, False, False)
    CP_SP = CarParamsSP()
    self.car_state = CarState(CP, CP_SP)
    self.can_parsers = CarState.get_can_parsers(CP, CP_SP)
    self.packer = CANPacker(DBC[CP.carFingerprint][Bus.party])
    self.frame = 0
    # CarState registers the messages it reads with the parsers on the first update.
    self.car_state.update(self.can_parsers)

  def update_blindspots(self, left: int, right: int):
    msg = self.packer.make_can_msg("DAS_status", CANBUS.autopilot_party, {
      "DAS_blindSpotRearLeft": left,
      "DAS_blindSpotRearRight": right,
    })
    self.frame += 1
    updated = self.can_parsers[Bus.ap_party].update([self.frame * 500_000_000, [msg]])
    self.assertIn(msg[0], updated)
    return self.car_state.update(self.can_parsers)[0]

  def test_blindspot_warning_levels(self):
    # DBC: NO_WARNING=0, WARNING_LEVEL_1=1, WARNING_LEVEL_2=2, SNA=3.
    expected = (False, True, True, False)
    for left, left_warning in enumerate(expected):
      for right, right_warning in enumerate(expected):
        with self.subTest(left=left, right=right):
          state = self.update_blindspots(left, right)
          self.assertEqual((state.leftBlindspot, state.rightBlindspot), (left_warning, right_warning))

  def test_blindspot_unavailable_clears_previous_warning(self):
    for left, right, expected in (
      (1, 2, (True, True)),
      (3, 2, (False, True)),
      (3, 3, (False, False)),
      (2, 1, (True, True)),
      (2, 3, (True, False)),
      (3, 3, (False, False)),
    ):
      with self.subTest(left=left, right=right):
        state = self.update_blindspots(left, right)
        self.assertEqual((state.leftBlindspot, state.rightBlindspot), expected)

  def test_steering_rate_uses_the_detected_bus(self):
    for vehicle_frames, ap_frames, expected_rate in (
      ({0x3DF: 8, 0x129: 8}, {}, -10),
      ({0x3DF: 8}, {}, -20),
      ({0x3DF: 8, 0x129: 6}, {}, -20),
      ({0x129: 8}, {}, -20),
      ({0x3DF: 8, 0x129: 8}, {0x293: 8}, -20),
    ):
      with self.subTest(vehicle_frames=vehicle_frames, ap_frames=ap_frames):
        fingerprint = gen_empty_fingerprint()
        fingerprint[CANBUS.vehicle].update(vehicle_frames)
        fingerprint[CANBUS.autopilot_party].update(ap_frames)
        CP = CarInterface.get_params(CAR.TESLA_MODEL_3, fingerprint, [], False, False, False)
        CP_SP = CarInterface.get_params_sp(CP, CAR.TESLA_MODEL_3, fingerprint, [], False, False, False)
        interface = CarInterface(CP, CP_SP)
        interface.update([])

        msg = "SCCM_steeringAngleSensor"
        signal = "SCCM_steeringAngleSpeed"
        vehicle = self.packer.make_can_msg(msg, CANBUS.vehicle, {signal: 10})
        ap = self.packer.make_can_msg(msg, CANBUS.autopilot_party, {signal: 20})
        state, _ = interface.update([(1_000_000_000, [vehicle, ap])])
        self.assertEqual(state.steeringRateDeg, expected_rate)
        steering_parser = interface.can_parsers[interface.CS.steering_rate_bus]
        self.assertEqual(steering_parser.message_states[0x129].timeout_override_nanos,
                         1_500_000_000 if expected_rate == -10 else None)

  def vehicle_steering_rate_parser(self):
    fingerprint = gen_empty_fingerprint()
    fingerprint[CANBUS.vehicle].update({0x3DF: 8, 0x129: 8})
    CP = CarInterface.get_params(CAR.TESLA_MODEL_3, fingerprint, [], False, False, False)
    CP_SP = CarInterface.get_params_sp(CP, CAR.TESLA_MODEL_3, fingerprint, [], False, False, False)
    interface = CarInterface(CP, CP_SP)
    interface.update([])
    packer = CANPacker(DBC[CP.carFingerprint][Bus.adas])
    return interface.can_parsers[Bus.adas], packer

  def test_vehicle_steering_rate_allows_150_missing_packets(self):
    vehicle_parser, packer = self.vehicle_steering_rate_parser()
    ui = packer.make_can_msg("UI_status2", CANBUS.vehicle, {})
    steering = packer.make_can_msg("SCCM_steeringAngleSensor", CANBUS.vehicle, {"SCCM_steeringAngleCounter": 1})
    vehicle_parser.update([(1_000_000_000, [ui, steering])])
    self.assertTrue(vehicle_parser.can_valid)

    for frame in range(1, 156):
      vehicle_parser.update([(1_000_000_000 + frame * 10_000_000, [ui])])
      # The normal five-invalid-check debounce follows the 150-packet allowance.
      self.assertEqual(vehicle_parser.can_valid, frame < 155, frame)
      self.assertFalse(vehicle_parser.bus_timeout)

    steering = packer.make_can_msg("SCCM_steeringAngleSensor", CANBUS.vehicle, {"SCCM_steeringAngleCounter": 2})
    vehicle_parser.update([(2_560_000_000, [ui, steering])])
    self.assertTrue(vehicle_parser.can_valid)

  def test_vehicle_steering_rate_still_requires_first_sample(self):
    vehicle_parser, packer = self.vehicle_steering_rate_parser()
    ui = packer.make_can_msg("UI_status2", CANBUS.vehicle, {})
    for frame in range(1, 161):
      vehicle_parser.update([(frame * 10_000_000, [ui])])
      self.assertFalse(vehicle_parser.can_valid)

  def test_vehicle_steering_rate_keeps_bus_timeout(self):
    vehicle_parser, packer = self.vehicle_steering_rate_parser()
    ui = packer.make_can_msg("UI_status2", CANBUS.vehicle, {})
    steering = packer.make_can_msg("SCCM_steeringAngleSensor", CANBUS.vehicle, {"SCCM_steeringAngleCounter": 1})
    vehicle_parser.update([(1_000_000_000, [ui, steering])])
    self.assertTrue(vehicle_parser.can_valid)
    for frame in range(1, 16):
      vehicle_parser.update([(1_000_000_000 + frame * 10_000_000, [])])
      self.assertEqual(vehicle_parser.bus_timeout, frame > 10, frame)

  def test_vehicle_steering_rate_keeps_counter_check(self):
    vehicle_parser, packer = self.vehicle_steering_rate_parser()
    ui = packer.make_can_msg("UI_status2", CANBUS.vehicle, {})
    steering = packer.make_can_msg("SCCM_steeringAngleSensor", CANBUS.vehicle, {"SCCM_steeringAngleCounter": 1})
    vehicle_parser.update([(1_000_000_000, [ui, steering])])
    self.assertTrue(vehicle_parser.can_valid)
    for frame in range(1, 6):
      vehicle_parser.update([(1_000_000_000 + frame * 10_000_000, [ui, steering])])
      self.assertEqual(vehicle_parser.can_valid, frame < 5, frame)

  def test_vehicle_steering_rate_does_not_extend_other_message_timeout(self):
    vehicle_parser, packer = self.vehicle_steering_rate_parser()
    ui = packer.make_can_msg("UI_status2", CANBUS.vehicle, {})
    for frame in range(1, 121):
      steering = packer.make_can_msg("SCCM_steeringAngleSensor", CANBUS.vehicle, {})
      vehicle_parser.update([(frame * 10_000_000, [ui, steering])])
      self.assertTrue(vehicle_parser.can_valid)

    for frame in range(1, 16):
      steering = packer.make_can_msg("SCCM_steeringAngleSensor", CANBUS.vehicle, {})
      vehicle_parser.update([(1_200_000_000 + frame * 10_000_000, [steering])])
      self.assertEqual(vehicle_parser.can_valid, frame < 15, frame)
      self.assertFalse(vehicle_parser.bus_timeout)
