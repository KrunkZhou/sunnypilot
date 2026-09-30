import unittest

from openpilot.sunnypilot.external_navigation.replay import compare_outputs, replay_policy


class ReplayTests(unittest.TestCase):
  def test_paired_metrics(self):
    neutral = [{'frameId': 1, 'positionY': [0., 1.], 'velocityX': [2., 3.], 'desiredAcceleration': .5}]
    hinted = [{'frameId': 1, 'positionY': [1., 2.], 'velocityX': [2., 3.], 'desiredAcceleration': .25}]
    report = compare_outputs(neutral, hinted)
    self.assertEqual(report['metrics']['positionY']['max_absolute_delta'], 1.)
    self.assertEqual(report['metrics']['desiredAcceleration']['mean_delta'], -.25)
    self.assertFalse(report['activation_approved'])

  def test_reject_invalid_comparisons(self):
    row = {'frameId': 1, 'positionY': [0.], 'velocityX': [2.], 'desiredAcceleration': .5}
    for bad in ([], [{**row, 'frameId': 2}], [row, row], [{**row, 'positionY': []}],
                [{**row, 'desiredAcceleration': float('nan')}]):
      with self.assertRaises(ValueError):
        compare_outputs([row], bad)

  def test_development_policy_replay_never_actuates(self):
    def row(distance, observation):
      return {'sample': {'identity': ['session', 1, 2, 3, 4], 'route_state': 1, 'maneuver_type': 2,
                         'distance': distance, 'observation': observation, 'distance_age_ms': 0},
              'vehicle': {'lateral_active': True, 'speed': 4., 'speed_limit': 8.}}
    output = list(replay_policy([row(30, 1), row(10, 2)]))
    self.assertEqual(output[-1]['proposal'], 2)
    self.assertTrue(all(r['desire'] == 0 for r in output))


if __name__ == '__main__':
  unittest.main()
