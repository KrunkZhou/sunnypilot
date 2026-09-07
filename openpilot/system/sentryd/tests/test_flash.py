import math

import pytest

from openpilot.system.sentryd.flash import FlashDetector, LightSample


def sample(at, on=False, *, right=None, hazards=False, fast=True):
  return LightSample(on, on if right is None else right, hazards, fast, round(at, 8))


def timeline(detector, pulses=(), *, start=0.0, end=3.0, step=0.02, hazards=()):
  results = []
  for index in range(round((end - start) / step) + 1):
    at = round(start + index * step, 8)
    on = any(begin <= at < finish for begin, finish in pulses)
    hazard = any(begin <= at < finish for begin, finish in hazards)
    result = detector.update(sample(at, on, hazards=hazard))
    if result is not None:
      results.append((at, result))
  return results


@pytest.mark.parametrize("pulses,result,finished", [
  ([(0.10, 0.50)], "locked", 2.50),
  ([(0.10, 0.50), (0.70, 1.10)], "unlocked", 3.10),
])
def test_single_and_double_require_completed_pulses_and_fresh_quiet(pulses, result, finished):
  detector = FlashDetector()
  assert timeline(detector, pulses, end=finished - 0.02) == []
  assert detector.pending
  assert detector.pulse_count == len(pulses)
  assert detector.update(sample(finished)) == result
  assert not detector.pending
  assert detector.error is None
  assert timeline(detector, start=finished + 0.02, end=finished + 1) == []


@pytest.mark.parametrize("width,accepted", [(0.10, True), (1.20, True), (0.08, False), (1.22, False)])
def test_pulse_duration_bounds(width, accepted):
  detector = FlashDetector()
  end = round(0.10 + width, 8)
  results = timeline(detector, [(0.10, end)], end=end + 2)
  assert results == ([(end + 2, "locked")] if accepted else [])


@pytest.mark.parametrize("gap,accepted", [(0.10, True), (1.50, True), (0.08, False), (1.52, False)])
def test_interpulse_gap_bounds(gap, accepted):
  detector = FlashDetector()
  second = round(0.30 + gap, 8)
  end = round(second + 0.20, 8)
  results = timeline(detector, [(0.10, 0.30), (second, end)], end=end + 2)
  assert results == ([(end + 2, "unlocked")] if accepted else [])


def test_third_pulse_rejects_entire_burst_without_recognizing_suffix():
  detector = FlashDetector()
  assert timeline(detector, [(0.10, 0.30), (0.50, 0.70), (0.90, 1.10), (1.30, 1.50)], end=3.60) == []
  assert not detector.pending
  assert timeline(detector, [(3.80, 4.00)], start=3.62, end=6.0) == [(6.0, "locked")]


def test_initial_on_never_counts_a_partial_single_or_double():
  detector = FlashDetector()
  assert timeline(detector, [(0.0, 0.30), (0.50, 0.70)], end=2.80) == []
  assert timeline(detector, [(3.0, 3.20)], start=2.82, end=5.20) == [(5.20, "locked")]


@pytest.mark.parametrize("hazards", [[(0.0, 1.30)], [(0.20, 0.22)], [(1.20, 1.22)]])
def test_hazards_anywhere_in_burst_or_confirmation_quiet_rejects_it(hazards):
  detector = FlashDetector()
  assert timeline(detector, [(0.10, 0.30), (0.50, 0.70)], end=3.40, hazards=hazards) == []


def test_normal_continuing_hazard_flashers_are_never_lock_events():
  detector = FlashDetector()
  pulses = [(round(index * 0.8, 8), round(index * 0.8 + 0.4, 8)) for index in range(20)]
  assert timeline(detector, pulses, end=16, hazards=[(0, 16.1)]) == []
  assert not detector.pending
  assert "Hazard" in detector.error


@pytest.mark.parametrize("at", [0.2, 0.6, 0.9])
def test_asymmetric_samples_invalidate_whole_burst(at):
  detector = FlashDetector()
  assert timeline(detector, [(0.10, 0.30)], end=at - 0.02) == []
  assert detector.update(sample(at, True, right=False)) is None
  assert "Asymmetric" in detector.error
  assert timeline(detector, [(at + 0.10, at + 0.30)], start=at + 0.02, end=at + 2.40) == []


@pytest.mark.parametrize("gap,valid", [(0.25, True), (0.2501, False)])
def test_fast_sample_gap_uses_previous_rate_even_when_returned_off_is_slow(gap, valid):
  detector = FlashDetector()
  detector.update(sample(0))
  detector.update(sample(0.1, True))
  detector.update(sample(0.1 + gap, False, fast=False))
  assert detector.pending is valid
  assert detector.pulse_count == (1 if valid else 0)
  assert detector.update(sample(1.1 + gap, fast=False)) is None
  assert detector.update(sample(2.1 + gap, fast=False)) == ("locked" if valid else None)


@pytest.mark.parametrize("gap,valid", [(1.5, True), (1.5001, False)])
def test_idle_gap_bounds(gap, valid):
  detector = FlashDetector()
  detector.update(sample(0, fast=False))
  detector.update(sample(gap, True))
  assert detector.pending is valid


def test_tick_cannot_infer_off_or_complete_sequence_without_new_samples():
  detector = FlashDetector()
  assert timeline(detector, [(0.10, 0.30)], end=0.40) == []
  assert detector.pending
  assert detector.tick(0.60) is None
  assert detector.pending
  assert detector.tick(2.30) is None
  assert not detector.pending
  assert "stale" in detector.error
  assert detector.update(sample(2.31)) is None
  assert detector.update(sample(2.40, True)) is None  # cannot recognize a suffix after the gap


def test_six_second_sequence_limit_includes_observed_confirmation_quiet():
  detector = FlashDetector()
  # Individually valid maximum pulses/gap need 5.9 seconds including quiet.
  assert timeline(detector, [(0.10, 1.30), (2.80, 4.00)], end=6.0) == [(6.0, "unlocked")]
  detector = FlashDetector()
  assert timeline(detector, [(0.10, 1.30), (2.80, 4.00)], end=5.98) == []
  # A late final observation cannot validate beyond six seconds even though its
  # declared idle send rate permits a longer sample interval.
  assert detector.update(sample(5.99, fast=False)) is None
  assert detector.update(sample(6.11, fast=False)) is None
  assert not detector.pending
  assert "six seconds" in detector.error


@pytest.mark.parametrize("bad", [
  None, object(), LightSample(1, False, False, False, 1.0), LightSample(False, False, False, False, float("nan")),
  LightSample(False, False, False, False, float("inf")), LightSample(False, False, False, False, -1.0),
])
def test_malformed_samples_invalidate_pending_burst(bad):
  detector = FlashDetector()
  timeline(detector, [(0.10, 0.30)], end=0.40)
  assert detector.update(bad) is None
  assert not detector.pending and detector.error is not None
  assert timeline(detector, [(0.60, 0.80)], start=0.42, end=2.80) == []


@pytest.mark.parametrize("timestamp", [0.4, 0.3])
def test_duplicate_and_reordered_samples_do_not_release_or_restart_candidate(timestamp):
  detector = FlashDetector()
  timeline(detector, [(0.10, 0.30)], end=0.40)
  assert detector.update(sample(timestamp)) is None
  assert "reordered" in detector.error
  assert timeline(detector, [(0.60, 0.80)], start=0.42, end=2.80) == []


def test_generation_reset_requires_quiet_before_counting_later_pulses():
  detector = FlashDetector()
  timeline(detector, [(0.10, 0.30)], end=0.40)
  detector.reset(require_quiet=True)
  assert timeline(detector, [(0.60, 0.80)], start=0.42, end=2.80) == []
  assert timeline(detector, [(3.0, 3.20)], start=2.82, end=5.20) == [(5.20, "locked")]


@pytest.mark.parametrize("now", [math.nan, math.inf, -1, "now"])
def test_invalid_tick_time_fails_closed(now):
  detector = FlashDetector()
  timeline(detector, [(0.10, 0.30)], end=0.40)
  assert detector.tick(now) is None
  assert not detector.pending and detector.error is not None
