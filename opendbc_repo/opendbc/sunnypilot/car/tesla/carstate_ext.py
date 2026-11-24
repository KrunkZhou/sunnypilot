"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from enum import StrEnum

from opendbc.car import Bus, create_button_events, structs
from opendbc.can.parser import CANParser
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.tesla.values import DBC, CANBUS
from opendbc.sunnypilot.car.tesla.values import TeslaFlagsSP

ButtonType = structs.CarState.ButtonEvent.Type
STEERING_RATE_FREQUENCY = 100
STEERING_RATE_TIMEOUT_PACKETS = 150
RIGHT_STALK_TIMEOUT_NANOS = 700_000_000


class CarStateExt:
  def __init__(self, CP: structs.CarParams, CP_SP: structs.CarParamsSP):
    self.CP = CP
    self.CP_SP = CP_SP

    self.active_touch_points = 0

  def update(self, ret: structs.CarState, ret_sp: structs.CarStateSP, can_parsers: dict[StrEnum, CANParser]) -> None:
    if self.CP_SP.flags & TeslaFlagsSP.HAS_VEHICLE_BUS:
      cp_adas = can_parsers[Bus.adas]

      if self.CP_SP.flags & TeslaFlagsSP.RIGHT_STALK_ON_VEHICLE_BUS:
        timestamps = cp_adas.message_states[0x229].timestamps
        # Use CAN monotonic time so replay and live detection have the same boundary.
        # Keep this separate from can_valid's debounce and require an initial sample.
        ret.teslaRightStalkTimeout = not timestamps or cp_adas._last_update_nanos - timestamps[-1] >= RIGHT_STALK_TIMEOUT_NANOS

      prev_active_touch_points = self.active_touch_points
      self.active_touch_points = int(cp_adas.vl["UI_status2"]["UI_activeTouchPoints"])

      finger_count = None
      if self.CP_SP.flags & TeslaFlagsSP.MADS_SCREEN_BUTTON_3_FINGER:
        finger_count = 3
      elif self.CP_SP.flags & TeslaFlagsSP.MADS_SCREEN_BUTTON_4_FINGER:
        finger_count = 4
      elif self.CP_SP.flags & TeslaFlagsSP.MADS_SCREEN_BUTTON_5_FINGER:
        finger_count = 5

      if finger_count is not None:
        ret.buttonEvents = [*create_button_events(self.active_touch_points, prev_active_touch_points,
                                                  {finger_count: ButtonType.lkas})]
    cp_party = can_parsers[Bus.party]

    cp_ap_party = can_parsers[Bus.ap_party]

    speed_units = self.can_define.dv["DI_state"]["DI_speedUnits"].get(int(cp_party.vl["DI_state"]["DI_speedUnits"]), None)
    speed_limit = cp_ap_party.vl["DAS_status"]["DAS_fusedSpeedLimit"]
    if self.can_define.dv["DAS_status"]["DAS_fusedSpeedLimit"].get(int(speed_limit), None) in ["NONE", "UNKNOWN_SNA"]:
      ret_sp.speedLimit = 0
    else:
      if speed_units == "KPH":
        ret_sp.speedLimit = speed_limit * CV.KPH_TO_MS
      elif speed_units == "MPH":
        ret_sp.speedLimit = speed_limit * CV.MPH_TO_MS

  @staticmethod
  def get_parser(CP: structs.CarParams, CP_SP: structs.CarParamsSP) -> dict[StrEnum, CANParser]:
    if CP_SP.flags & TeslaFlagsSP.HAS_VEHICLE_BUS:
      vehicle_steering_rate = CP_SP.flags & TeslaFlagsSP.STEERING_RATE_ON_VEHICLE_BUS
      messages = [("SCCM_steeringAngleSensor", STEERING_RATE_FREQUENCY)] if vehicle_steering_rate else []
      if CP_SP.flags & TeslaFlagsSP.RIGHT_STALK_ON_VEHICLE_BUS:
        # The dedicated freshness check cancels cruise only. Do not also turn a missing
        # stalk into a generic CAN error that would disengage MADS steering.
        messages.append(("SCCM_rightStalk", float("nan")))
      parser = CANParser(DBC[CP.carFingerprint][Bus.adas], messages, CANBUS.vehicle)
      if vehicle_steering_rate:
        parser.message_states[0x129].timeout_override_nanos = int(1e9 * STEERING_RATE_TIMEOUT_PACKETS / STEERING_RATE_FREQUENCY)
      return {Bus.adas: parser}
    return {}
