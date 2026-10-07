"""Offline-only modeld launcher for recorded-camera comparisons; never registered with manager."""
import os


class ReplayHints:
  def __init__(self):
    self.frame = int(os.environ['EXTERNAL_NAV_REPLAY_FRAME'])
    self.hint = int(os.environ['EXTERNAL_NAV_REPLAY_HINT'])
    if self.hint not in (0, 1, 2, 6):
      raise ValueError('Only neutral, turnLeft, turnRight and keepRight are valid research inputs')
    self.consumed = False
    self.output = 0

  def update(self, sm, desire_helper, model):
    self.output = 0
    if not self.consumed and sm['narrowRoadCameraState'].frameId == self.frame:
      self.output = self.hint
      self.consumed = True
    return self.output

  def model_completed(self, desire: int, effective_pulse) -> None:
    """Accept the model runner callback without reporting live navigation assistance."""

  def fill(self, message):
    message.navigationHint = self.output
    message.navigationHintStatus = 'offline_recorded_camera_research'
    message.navigationAssisted = False


def main():
  from openpilot.common.hardware import PC
  if not PC or os.environ.get('REPLAY') != '1' or os.environ.get('SIMULATION') != '1':
    raise RuntimeError('Recorded-camera research launcher requires a PC and the isolated process replay harness')
  pipeline = os.environ.get('EXTERNAL_NAV_REPLAY_PIPELINE', 'stock')
  if pipeline == 'stock':
    from openpilot.selfdrive.modeld import modeld
  elif pipeline == 'tinygrad':
    from openpilot.sunnypilot.modeld_v2 import modeld
  else:
    raise ValueError('Unknown model pipeline')
  modeld.ExternalNavigationHints = ReplayHints
  modeld.main()


if __name__ == '__main__':
  main()
