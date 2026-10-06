"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

from openpilot.sunnypilot.selfdrive.controls.lib.nnlc.nnlc import NeuralNetworkLateralControl
from openpilot.sunnypilot.selfdrive.controls.lib.latcontrol_torque_ext_override import LatControlTorqueExtOverride


class LatControlTorqueExt(NeuralNetworkLateralControl, LatControlTorqueExtOverride):
  def __init__(self, lac_torque, CP, CP_SP, CI):
    NeuralNetworkLateralControl.__init__(self, lac_torque, CP, CP_SP, CI)
    LatControlTorqueExtOverride.__init__(self, CP)
    self._controller_mode = None

  def select_pid(self, base_pid):
    mode = "nnlc" if self._nnlc_enabled else "jerk" if self._jerk_aware_enabled else "base"
    if mode != self._controller_mode:
      # Never transfer an integral between acceleration-space and torque-space control.
      self.reset()
      base_pid.reset()
      self._controller_mode = mode
    self._pid.set_limits(self.lac_torque.steer_max, -self.lac_torque.steer_max)
    return base_pid if mode == "base" else self._pid

  def update(self, CS, VM, params, ff, pid_log, setpoint, measurement, calibrated_pose, roll_compensation,
             desired_lateral_accel, actual_lateral_accel, lateral_accel_deadzone, gravity_adjusted_lateral_accel,
             desired_curvature, actual_curvature, steer_limited_by_safety, output_torque):
    if not (self._nnlc_enabled or self._jerk_aware_enabled):
      return pid_log, output_torque
    self._ff = ff
    self._pid_log = pid_log
    self._setpoint = setpoint
    self._measurement = measurement
    self._roll_compensation = roll_compensation
    self._lateral_accel_deadzone = lateral_accel_deadzone
    self._desired_lateral_accel = desired_lateral_accel
    self._actual_lateral_accel = actual_lateral_accel
    self._desired_curvature = desired_curvature
    self._actual_curvature = actual_curvature
    self._gravity_adjusted_lateral_accel = gravity_adjusted_lateral_accel
    self._steer_limited_by_safety = steer_limited_by_safety
    self._output_torque = output_torque

    self.update_calculations(CS, VM, desired_lateral_accel)
    # NNLC supersedes jerk-aware torque control; integrate exactly once per tick.
    if self._nnlc_enabled:
      self.update_neural_network_feedforward(CS, params, calibrated_pose)
    else:
      self.update_jerk_aware_torque_control(CS, roll_compensation, gravity_adjusted_lateral_accel)

    return self._pid_log, self._output_torque
