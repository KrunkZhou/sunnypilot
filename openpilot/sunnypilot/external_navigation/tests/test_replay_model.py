import os
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from openpilot.sunnypilot.external_navigation import replay_model


class ReplayModelTests(unittest.TestCase):
  def test_both_pipeline_hooks_keep_recorded_hint_one_shot_and_unassisted(self):
    for pipeline, package in (('stock', 'openpilot.selfdrive.modeld'), ('tinygrad', 'openpilot.sunnypilot.modeld_v2')):
      for hint in (0, 1, 2, 6):
        with self.subTest(pipeline=pipeline, hint=hint):
          messages = []
          runner = ModuleType(f'{package}.modeld')

          def run_model():
            self.assertIs(runner.ExternalNavigationHints, replay_model.ReplayHints)
            hints = runner.ExternalNavigationHints()
            for frame in (99, 100, 100, 101):
              desire = hints.update({'narrowRoadCameraState': SimpleNamespace(frameId=frame)}, None, runner)
              pulse = [float(index == desire) for index in range(8)]
              hints.model_completed(desire, pulse)
              message = SimpleNamespace(navigationAssisted=True, navigationTurnEvent=0)
              hints.fill(message)
              messages.append(message)

          runner.main = run_model
          module = ModuleType(package)
          module.modeld = runner
          hardware = ModuleType('openpilot.common.hardware')
          hardware.PC = True
          env = {'EXTERNAL_NAV_REPLAY_FRAME': '100', 'EXTERNAL_NAV_REPLAY_HINT': str(hint),
                 'EXTERNAL_NAV_REPLAY_PIPELINE': pipeline, 'REPLAY': '1', 'SIMULATION': '1'}
          with patch.dict(os.environ, env), patch.dict(sys.modules, {package: module, 'openpilot.common.hardware': hardware}):
            replay_model.main()

          self.assertEqual([message.navigationHint for message in messages], [0, hint, 0, 0])
          self.assertTrue(all(message.navigationHintStatus == 'offline_recorded_camera_research' for message in messages))
          self.assertTrue(all(not message.navigationAssisted and message.navigationTurnEvent == 0 for message in messages))


if __name__ == '__main__':
  unittest.main()
