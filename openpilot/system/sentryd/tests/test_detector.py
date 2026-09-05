import pytest

from openpilot.system.sentryd.config import SENSITIVITY_TO_THRESHOLD
from openpilot.system.sentryd.detector import MotionDetector


@pytest.mark.parametrize("threshold, sample_delta, expected_events", [
  pytest.param(SENSITIVITY_TO_THRESHOLD["high"], 0.015, [], id="high-ignores-smaller-motion"),
  pytest.param(0.01, 0.015, ["motion", "warning"], id="temporary-high-detected-smaller-motion"),
  pytest.param(SENSITIVITY_TO_THRESHOLD["high"], 0.03, ["motion", "warning"], id="high-detects-qualifying-motion"),
  pytest.param(SENSITIVITY_TO_THRESHOLD["standard"], 0.03, [], id="standard"),
  pytest.param(SENSITIVITY_TO_THRESHOLD["low"], 0.03, [], id="low"),
])
def test_restored_high_threshold_for_repeated_motion(threshold: float, sample_delta: float, expected_events: list[str]) -> None:
  detector = MotionDetector(threshold_mps2=threshold, warning_persistence_seconds=1.0)
  assert detector.update((0.0, 0.0, 9.81), 0.0) is None

  # Restored High ignores 0.015 m/s² differences, unlike the temporary 0.01
  # setting, but still detects 0.03 m/s² differences that Standard and Low ignore.
  events = []
  for index in range(1, 11):
    event = detector.update((sample_delta * (index % 2), 0.0, 9.81), index / 10)
    if event is not None:
      events.append(event)
  assert events == expected_events
  assert detector.episode_active is bool(expected_events)


def test_high_ignores_subthreshold_three_axis_noise() -> None:
  detector = MotionDetector(threshold_mps2=SENSITIVITY_TO_THRESHOLD["high"], warning_persistence_seconds=1.0)
  assert detector.update((0.0, 0.0, 9.81), 0.0) is None

  # The full vector difference is about 0.01709 m/s², below 0.02 m/s².
  for index in range(1, 101):
    sample = (0.012, 0.012, 9.812) if index % 2 else (0.0, 0.0, 9.81)
    assert detector.update(sample, index / 10) is None
  assert detector.motion_evidence_seconds == 0.0
  assert not detector.episode_active


@pytest.mark.parametrize("warning_persistence_seconds", [0.5, 1.0, 2.0, 5.0])
def test_first_qualifying_sample_starts_episode_without_waiting_for_warning(warning_persistence_seconds: float) -> None:
  detector = MotionDetector(threshold_mps2=0.1, warning_persistence_seconds=warning_persistence_seconds)
  assert detector.update((0.0, 0.0, 9.81), 0.0) is None
  assert detector.update((0.01, 0.0, 9.81), 0.1) is None
  assert not detector.episode_active

  assert detector.update((0.21, 0.0, 9.81), 0.2) == "motion"
  assert detector.episode_active
  assert not detector.warning_triggered
  assert detector.motion_evidence_seconds == pytest.approx(0.1)
  assert detector.first_motion_at == pytest.approx(0.2)
  assert detector.last_motion_at == pytest.approx(0.2)


def test_first_motion_can_also_satisfy_warning_without_an_extra_event() -> None:
  detector = MotionDetector(threshold_mps2=0.1, warning_persistence_seconds=0.1)
  assert detector.update((0.0, 0.0, 0.0), 0.0) is None
  assert detector.update((1.0, 0.0, 0.0), 0.1) == "motion"
  assert detector.warning_triggered
  assert detector.update((0.0, 0.0, 0.0), 0.2) is None


def test_uses_vector_difference_not_scalar_magnitude_difference() -> None:
  detector = MotionDetector(threshold_mps2=1.0, warning_persistence_seconds=0.1)
  detector.update((1.0, 0.0, 0.0), 0.0)
  # Equal scalar magnitudes but a large directional vector change.
  assert detector.update((0.0, 1.0, 0.0), 0.1) == "motion"


@pytest.mark.parametrize("as_reader", [False, True])
def test_accepts_capnp_accelerometer_vectors(as_reader) -> None:
  from openpilot.cereal import log

  event = log.SensorEventData.new_message(acceleration={"v": [0.0, 0.0, 9.81]})
  sample = event.as_reader() if as_reader else event
  detector = MotionDetector(warning_persistence_seconds=0.1)
  assert detector.update(sample.acceleration.v, 0.0) is None

  # The real sensor list supports integer indexing but not Python slices.
  # Updating the message must also leave the detector's previous sample intact.
  event.acceleration.v[0] = 0.1
  assert detector.update(sample.acceleration.v, 0.1) == "motion"
  assert detector.previous_acceleration == pytest.approx((0.1, 0.0, 9.81))


def test_evidence_accumulates_and_decays() -> None:
  detector = MotionDetector(threshold_mps2=0.1, warning_persistence_seconds=0.3)
  detector.update((0.0, 0.0, 0.0), 0.0)
  assert detector.update((1.0, 0.0, 0.0), 0.1) == "motion"
  assert detector.update((1.0, 0.0, 0.0), 0.2) is None
  assert detector.motion_evidence_seconds == pytest.approx(0.0)
  assert detector.episode_active
  assert not detector.warning_triggered
  assert detector.update((0.0, 0.0, 0.0), 0.3) is None
  assert detector.update((1.0, 0.0, 0.0), 0.4) is None
  assert detector.update((0.0, 0.0, 0.0), 0.5) == "warning"
  assert detector.warning_triggered
  assert detector.update((1.0, 0.0, 0.0), 0.6) is None


def test_one_episode_promotes_to_alarm_then_closes_after_quiet() -> None:
  detector = MotionDetector(
    threshold_mps2=0.1,
    warning_persistence_seconds=0.2,
    alarm_persistence_seconds=0.5,
    quiet_close_seconds=0.5,
  )
  detector.update((0.0, 0.0, 0.0), 0.0)
  events = []
  value = 0.0
  for index in range(1, 7):
    value = 1.0 - value
    event = detector.update((value, 0.0, 0.0), index / 10)
    if event:
      events.append(event)
  assert events == ["motion", "warning", "alarm"]
  assert detector.update((value, 0.0, 0.0), 1.2) == "closed"
  assert not detector.episode_active
  assert not detector.warning_triggered
  assert not detector.alarm_triggered


def test_rejects_invalid_samples() -> None:
  detector = MotionDetector()
  with pytest.raises(ValueError):
    detector.update((1.0, 2.0))
  with pytest.raises(ValueError):
    detector.update((1.0, 2.0, float("nan")))


def test_active_episode_closes_during_accelerometer_gap() -> None:
  detector = MotionDetector(threshold_mps2=0.1, warning_persistence_seconds=0.1, quiet_close_seconds=1.0)
  detector.update((0.0, 0.0, 0.0), 0.0)
  assert detector.update((1.0, 0.0, 0.0), 0.1) == "motion"
  assert detector.tick(1.09) is None
  assert detector.tick(1.1) == "closed"
  assert not detector.episode_active


def test_sensor_gap_discards_partial_motion_evidence() -> None:
  detector = MotionDetector(threshold_mps2=0.1, warning_persistence_seconds=1.0, sample_stale_seconds=1.0)
  detector.update((0.0, 0.0, 0.0), 0.0)
  value = 0.0
  for index in range(1, 10):
    value = 1.0 - value
    assert detector.update((value, 0.0, 0.0), index / 10) == ("motion" if index == 1 else None)
  assert detector.motion_evidence_seconds == pytest.approx(0.9)
  detector.tick(2.0)
  assert detector.motion_evidence_seconds == 0
  assert detector.episode_active
  assert not detector.warning_triggered
  assert detector.first_motion_at == pytest.approx(0.1)
  assert detector.last_motion_at == pytest.approx(0.9)
  assert detector.update((1.0, 0.0, 0.0), 2.1) is None
  # A fresh baseline cannot bridge the sensor gap or create another capture
  # episode. Only fresh differences can rebuild warning evidence.
  assert detector.update((0.0, 0.0, 0.0), 2.2) is None
  assert detector.motion_evidence_seconds == pytest.approx(0.1)


def test_motion_episode_can_close_without_ever_reaching_warning() -> None:
  detector = MotionDetector(threshold_mps2=0.1, warning_persistence_seconds=5.0, quiet_close_seconds=1.0)
  assert detector.update((0.0, 0.0, 0.0), 0.0) is None
  assert detector.update((1.0, 0.0, 0.0), 0.1) == "motion"
  assert detector.update((1.0, 0.0, 0.0), 0.2) is None
  assert detector.motion_evidence_seconds == pytest.approx(0.0)
  assert not detector.warning_triggered
  assert detector.tick(1.1) == "closed"
  assert not detector.episode_active

  assert detector.update((1.0, 0.0, 0.0), 1.2) is None
  assert detector.update((0.0, 0.0, 0.0), 1.3) == "motion"


def test_warning_and_alarm_are_not_repeated_after_a_sensor_gap() -> None:
  detector = MotionDetector(
    threshold_mps2=0.1, warning_persistence_seconds=0.2, alarm_persistence_seconds=0.3, sample_stale_seconds=1.0,
  )
  assert detector.update((0.0, 0.0, 0.0), 0.0) is None
  assert detector.update((1.0, 0.0, 0.0), 0.1) == "motion"
  assert detector.update((0.0, 0.0, 0.0), 0.2) == "warning"
  assert detector.update((1.0, 0.0, 0.0), 0.3) == "alarm"
  assert detector.tick(2.0) is None
  assert detector.warning_triggered
  assert detector.alarm_triggered

  assert detector.update((0.0, 0.0, 0.0), 2.1) is None
  for index in range(1, 6):
    assert detector.update((float(index % 2), 0.0, 0.0), 2.1 + index / 10) is None
