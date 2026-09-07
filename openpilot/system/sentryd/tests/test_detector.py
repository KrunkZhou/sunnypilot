import pytest

from openpilot.system.sentryd.config import SENSITIVITY_TO_THRESHOLD
from openpilot.system.sentryd.detector import MotionDetector


def hits(detector, count, *, start=0.1):
  events = []
  for index in range(count):
    previous = detector.previous_acceleration[0]
    event = detector.update((1.0 - previous, 0.0, 0.0), round(start + index / 10, 9))
    if event:
      events.append(event)
  return events


@pytest.mark.parametrize("threshold, sample_delta, expected", [
  (SENSITIVITY_TO_THRESHOLD["high"], 0.015, []),
  (SENSITIVITY_TO_THRESHOLD["high"], 0.03, ["motion", "warning"]),
  (SENSITIVITY_TO_THRESHOLD["standard"], 0.03, []),
  (SENSITIVITY_TO_THRESHOLD["low"], 0.03, []),
])
def test_presets_count_changes_in_acceleration_magnitude(threshold, sample_delta, expected):
  detector = MotionDetector(threshold_mps2=threshold)
  assert detector.update((0.0, 0.0, 9.81), 0.0) is None
  events = []
  for index in range(1, 11):
    event = detector.update((0.0, 0.0, 9.81 + sample_delta * (index % 2)), index / 10)
    if event:
      events.append(event)
  assert events == expected
  assert detector.trigger_count == (10 if expected else 0)


def test_equal_magnitude_direction_changes_do_not_count():
  detector = MotionDetector(threshold_mps2=0.02)
  assert detector.update((1.0, 0.0, 0.0), 0.0) is None
  for index, sample in enumerate(((0.0, 1.0, 0.0), (0.0, 0.0, -1.0), (-1.0, 0.0, 0.0)), 1):
    assert detector.update(sample, index / 10) is None
  assert detector.trigger_count == 0 and not detector.episode_active


def test_threshold_is_strictly_greater_not_greater_equal():
  detector = MotionDetector(threshold_mps2=0.5)
  detector.update((1.0, 0.0, 0.0), 0.0)
  assert detector.update((1.5, 0.0, 0.0), 0.1) is None
  assert detector.update((2.01, 0.0, 0.0), 0.2) == "motion"
  assert detector.trigger_count == 1


def test_first_hit_is_provisional_and_tenth_confirms_exactly_once():
  detector = MotionDetector()
  assert detector.update((0.0, 0.0, 0.0), 0.0) is None
  assert hits(detector, 1) == ["motion"]
  assert detector.trigger_count == 1 and not detector.warning_triggered
  assert hits(detector, 8, start=0.2) == []
  assert detector.trigger_count == 9 and not detector.warning_triggered
  assert hits(detector, 1, start=1.0) == ["warning"]
  assert detector.trigger_count == 10 and detector.warning_triggered
  assert hits(detector, 15, start=1.1) == []


def test_quiet_samples_do_not_decay_hits_or_create_new_episodes():
  detector = MotionDetector()
  detector.update((0.0, 0.0, 0.0), 0.0)
  assert hits(detector, 5) == ["motion"]
  value = detector.previous_acceleration
  for index in range(6, 101):
    assert detector.update(value, index / 10) is None
  assert detector.trigger_count == 5 and detector.first_motion_at == 0.1
  assert hits(detector, 5, start=10.1) == ["warning"]


def test_alarm_requires_more_than_twenty_five_hits_and_thirty_seconds():
  detector = MotionDetector()
  detector.update((0.0, 0.0, 0.0), 0.0)
  assert hits(detector, 24) == ["motion", "warning"]
  assert not detector.alarm_triggered
  value = detector.previous_acceleration
  for index in range(25, 302):
    assert detector.update(value, index / 10) is None
  assert hits(detector, 1, start=30.2) == []
  assert detector.trigger_count == 25 and not detector.alarm_triggered
  assert hits(detector, 1, start=30.3) == ["alarm"]
  assert detector.trigger_count == 26
  assert hits(detector, 5, start=30.4) == []


def test_more_than_twenty_five_early_hits_still_wait_for_thirty_seconds():
  detector = MotionDetector()
  detector.update((0.0, 0.0, 0.0), 0.0)
  assert hits(detector, 26) == ["motion", "warning"]
  assert not detector.alarm_triggered
  value = detector.previous_acceleration
  for index in range(27, 301):
    assert detector.update(value, index / 10) is None
  assert hits(detector, 1, start=30.1) == ["alarm"]


@pytest.mark.parametrize("confirmed", [False, True])
def test_sixty_second_window_closes_even_with_recent_motion(confirmed):
  detector = MotionDetector()
  detector.update((0.0, 0.0, 0.0), 0.0)
  assert hits(detector, 10 if confirmed else 9) == (["motion", "warning"] if confirmed else ["motion"])
  value = detector.previous_acceleration
  for index in range(11 if confirmed else 10, 601):
    assert detector.update(value, index / 10) is None
  # Expiry wins when an otherwise qualifying tenth hit lands at the deadline.
  assert detector.update((1.0 - value[0], 0.0, 0.0), 60.1) == "closed"
  assert detector.trigger_count == 0 and not detector.episode_active


def test_tenth_hit_just_before_deadline_is_confirmed():
  detector = MotionDetector()
  detector.update((0.0, 0.0, 0.0), 0.0)
  assert hits(detector, 9) == ["motion"]
  value = detector.previous_acceleration
  for index in range(10, 601):
    assert detector.update(value, index / 10) is None
  assert detector.update((1.0 - value[0], 0.0, 0.0), 60.099) == "warning"
  assert detector.tick(60.1) == "closed"


def test_sensor_gap_discards_unconfirmed_hits_and_requires_new_baseline():
  detector = MotionDetector()
  detector.update((0.0, 0.0, 0.0), 0.0)
  assert hits(detector, 9) == ["motion"]
  assert detector.tick(2.0) == "discarded"
  assert not detector.episode_active and detector.trigger_count == 0
  assert detector.update((1.0, 0.0, 0.0), 2.1) is None
  assert detector.update((0.0, 0.0, 0.0), 2.2) == "motion"
  assert detector.trigger_count == 1


def test_confirmed_gap_preserves_count_but_does_not_compare_across_gap():
  detector = MotionDetector()
  detector.update((0.0, 0.0, 0.0), 0.0)
  assert hits(detector, 10) == ["motion", "warning"]
  assert detector.tick(2.0) is None
  assert detector.warning_triggered and detector.trigger_count == 10
  assert detector.previous_acceleration is None
  assert detector.update((20.0, 0.0, 0.0), 2.1) is None
  assert detector.trigger_count == 10
  assert detector.update((21.0, 0.0, 0.0), 2.2) is None
  assert detector.trigger_count == 11


@pytest.mark.parametrize("as_reader", [False, True])
def test_accepts_capnp_accelerometer_vectors_without_slicing(as_reader):
  from openpilot.cereal import log
  event = log.SensorEventData.new_message(acceleration={"v": [0.0, 0.0, 9.81]})
  sample = event.as_reader() if as_reader else event
  detector = MotionDetector()
  assert detector.update(sample.acceleration.v, 0.0) is None
  event.acceleration.v[2] = 9.91
  assert detector.update(sample.acceleration.v, 0.1) == "motion"
  assert detector.previous_acceleration == pytest.approx((0.0, 0.0, 9.91))


@pytest.mark.parametrize("sample", [(1.0, 2.0), (1.0, 2.0, float("nan")), (1.0, 2.0, float("inf"))])
def test_rejects_invalid_samples(sample):
  with pytest.raises(ValueError):
    MotionDetector().update(sample)
