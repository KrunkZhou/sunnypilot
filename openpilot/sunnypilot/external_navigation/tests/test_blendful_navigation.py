import ast
from functools import partial
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from openpilot.sunnypilot.external_navigation.policy import Sample, TurnPolicy
from openpilot.sunnypilot.external_navigation.replay_model import ReplayHints
from openpilot.sunnypilot.modeld_v2.constants import ModelConstants


class BlendfulNavigationTests(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    repo = Path(__file__).resolve().parents[3]
    realtime = ast.parse((repo / 'common/realtime.py').read_text())
    dt = next(node for node in realtime.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == 'DT_MDL' for target in node.targets))
    # Execute the real correction code without importing native hardware through realtime.
    namespace = {'DT_MDL': ast.literal_eval(dt.value)}
    source = repo / 'sunnypilot/modeld_v2/blendful.py'
    tree = ast.parse(source.read_text())
    tree.body = [node for node in tree.body if not (isinstance(node, ast.ImportFrom) and node.module == 'openpilot.common.realtime')]
    exec(compile(tree, str(source), 'exec'), namespace)
    cls.Blendful = namespace['Blendful']

    source = repo / 'sunnypilot/modeld_v2/modeld.py'
    tree = ast.parse(source.read_text())
    callback = next(node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'partial' and ast.unparse(node.args[0]) == 'blendful.update')
    cls.callback = compile(ast.Expression(callback), str(source), 'eval')

  @staticmethod
  def frame(curvature=0.0, center=0.0):
    x = np.array(ModelConstants.X_IDXS)
    lines = np.zeros((1, 4, len(x), 2))
    for i, offset in enumerate((-5.25, -1.75, 1.75, 5.25)):
      lines[0, i, :, 0] = center + offset
    plan = np.zeros((1, len(x), ModelConstants.PLAN_WIDTH))
    plan[0, :, 0], plan[0, :, 1] = x, curvature * x ** 2 / 2
    return {'lane_lines': lines, 'lane_lines_stds': np.full_like(lines, 0.1),
            'lane_lines_prob': np.full((1, 8), 0.9), 'plan': plan}

  def correction(self, blendful, hints, desire, output, curvature, enabled=True):
    callback = eval(self.callback, {'partial': partial, 'blendful': blendful, 'navigation_hints': hints,
                                  'model_output': output, 'v_ego': 7.0, 'blendful_active': desire == 0,
                                  'blendful_enabled': enabled})
    return callback(curvature)

  def test_assisted_mode_clears_existing_correction_and_stays_off_after_pulse(self):
    blendful, hints, policy = self.Blendful(), SimpleNamespace(mode=0), TurnPolicy()
    for _ in range(100):
      self.correction(blendful, hints, 0, self.frame(center=0.5), 0.0)
    self.assertGreater(blendful.curvature, 0.0)

    hints.mode = 1
    policy.update(Sample(('session', 1, 1, 1, 1), 1, 2, 20, 1, 0),
                  mode=1, lateral_active=True, speed=7.0, speed_limit=8.0)
    desires, corrections = [], []
    for _ in range(41):
      decision = policy.update(Sample(('session', 1, 1, 1, 1), 1, 2, 15, 2, 0),
                               mode=1, lateral_active=True, speed=7.0, speed_limit=8.0)
      desires.append(decision.desire)
      corrections.append(self.correction(blendful, hints, decision.desire, self.frame(curvature=0.02), 0.02))
    self.assertEqual(desires, [2] + [0] * 40)
    self.assertEqual(corrections, [0.0] * 41)
    self.assertEqual(blendful.curvature, 0.0)
    self.assertEqual(blendful.trust, 0.0)

  def test_navigation_off_retains_configured_blendful_behavior(self):
    hints = SimpleNamespace(mode=0)
    for enabled in (False, True):
      with self.subTest(enabled=enabled):
        actual, upstream = self.Blendful(), self.Blendful()
        for _ in range(100):
          output = self.frame(center=0.5)
          value = self.correction(actual, hints, 0, output, 0.0, enabled)
          self.assertEqual(value, upstream.update(output, 7.0, True, 0.0, enabled=enabled))
        self.assertGreater(value, 0.0) if enabled else self.assertEqual(value, 0.0)

  def test_recorded_neutral_and_hint_runs_keep_correction_disabled(self):
    for hint in (0, 1, 2, 6):
      with self.subTest(hint=hint), patch.dict('os.environ', {'EXTERNAL_NAV_REPLAY_FRAME': '100', 'EXTERNAL_NAV_REPLAY_HINT': str(hint)}):
        hints, blendful = ReplayHints(), self.Blendful()
        for frame_id in (99, 100, 100, 101):
          desire = hints.update({'narrowRoadCameraState': SimpleNamespace(frameId=frame_id)}, None, None)
          self.assertEqual(self.correction(blendful, hints, desire, self.frame(curvature=0.02), 0.02), 0.0)


if __name__ == '__main__':
  unittest.main()
