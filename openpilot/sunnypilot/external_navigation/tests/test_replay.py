import unittest

from openpilot.sunnypilot.external_navigation.replay import compare_outputs, replay_policy, replay_speed


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

  def speed_row(self, now_ns=10_000_000_000, distance=100., observation=1, **vehicle):
    return {'now_ns': now_ns,
            'sample': {'identity': ['70' * 16, 1, 2, 3, 42], 'transport': ['72' * 16, 1],
                       'route_state': 1, 'maneuver_type': 1, 'distance': distance,
                       'distance_observation': observation, 'maneuver_observation': 1,
                       'distance_age_ms': 0, 'maneuver_age_ms': 0, 'delivery_age_ms': 0},
            'vehicle': {'v_ego': 80 / 3.6, 'baseline_target': 80 / 3.6, **vehicle}}

  def test_speed_replay_is_deterministic_and_serializable(self):
    import json
    rows = [self.speed_row(), self.speed_row(10_100_000_000, 90., 2),
            self.speed_row(10_200_000_000, 85., 3, enabled=False)]
    first = list(replay_speed(rows))
    second = list(replay_speed(json.loads(json.dumps(rows))))
    self.assertEqual(first, second)
    self.assertLess(first[1]['speed_cap'], first[0]['speed_cap'])
    self.assertIsNone(first[2]['speed_cap'])
    self.assertTrue(all(not row['activation_approved'] for row in first))
    json.dumps(first, allow_nan=False)
    self.assertEqual(rows[0]['sample']['identity'][0], '70' * 16)  # Does not mutate input rows.

  def test_speed_replay_missing_sample_removes_cap(self):
    output = list(replay_speed([self.speed_row(), {**self.speed_row(10_100_000_000), 'sample': None}]))
    self.assertIsNotNone(output[0]['speed_cap'])
    self.assertIsNone(output[1]['speed_cap'])

  def test_speed_replay_requires_valid_monotonic_clock(self):
    for timestamp in (-1, 1.5, True, '100'):
      with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
        list(replay_speed([self.speed_row(timestamp)]))
    with self.assertRaises(ValueError):
      list(replay_speed([self.speed_row(), self.speed_row(9_000_000_000)]))

  def test_speed_replay_repeated_snapshot_still_expires(self):
    output = list(replay_speed([self.speed_row(), self.speed_row(11_001_000_000)]))
    self.assertIsNotNone(output[0]['speed_cap'])
    self.assertIsNone(output[1]['speed_cap'])


if __name__ == '__main__':
  unittest.main()
