import datetime
import json
import logging
from types import SimpleNamespace

import pytest

from openpilot.common import diagnostic_timing as timing
from openpilot.common.logging_extra import SwagFormatter, SwagLogFileFormatter, SwagLogger

BOOT_ID = "c08e8b38-8f57-4f39-a8c0-8bf894992b54"
WALL_NS = 1_790_394_870_545_273_123
MONO_NS = 48_104_948_503


@pytest.fixture(autouse=True)
def reset_boot_cache():
  previous_factory = logging.getLogRecordFactory()
  timing.get_boot_id.cache_clear()
  yield
  logging.setLogRecordFactory(previous_factory)
  timing.get_boot_id.cache_clear()


@pytest.mark.parametrize("value", [BOOT_ID + "\n", "invalid", "00000000-0000-0000-0000-000000000000", BOOT_ID.replace("-", ""), ""])
def test_kernel_boot_identity_only(monkeypatch, value):
  monkeypatch.setattr(timing, "Path", lambda path: SimpleNamespace(read_text=lambda: value))
  assert timing.get_boot_id() == (BOOT_ID if value == BOOT_ID + "\n" else None)


def test_missing_boot_identity_does_not_generate_a_fallback(monkeypatch):
  def unreadable():
    raise PermissionError("no procfs")
  monkeypatch.setattr(timing, "Path", lambda path: SimpleNamespace(read_text=unreadable))
  monkeypatch.setattr(timing, "time", SimpleNamespace(time_ns=lambda: WALL_NS, monotonic_ns=lambda: MONO_NS))
  captured = timing.capture_timing()
  assert captured.log_fields() == {"wall_time_ns": str(WALL_NS), "monotonic_ns": str(MONO_NS)}
  assert captured.stats_fields() == f"rtzs_monotonic_ns={MONO_NS}i"
  assert timing.clock_jump(1, 2, MONO_NS) is None


def test_python_log_records_keep_producer_timing_through_delayed_formatting(monkeypatch):
  monkeypatch.setattr(timing, "get_boot_id", lambda: BOOT_ID)
  monkeypatch.setattr(timing, "time", SimpleNamespace(time_ns=lambda: WALL_NS, monotonic_ns=lambda: MONO_NS))
  logger = SwagLogger()
  records = []
  handler = logging.Handler()
  handler.emit = records.append
  logger.addHandler(handler)
  logger.warning("clock-independent record")
  record = records[0]
  created = record.created
  monkeypatch.setattr(timing, "time", SimpleNamespace(time_ns=lambda: WALL_NS + 99_000_000_000, monotonic_ns=lambda: MONO_NS + 99_000_000_000))
  wire = SwagFormatter(logger).format(record)
  file_record = json.loads(SwagLogFileFormatter(None).format(wire))
  assert file_record["created"] == created
  assert file_record["boot_id"] == BOOT_ID
  assert file_record["monotonic_ns"] == str(MONO_NS)
  assert file_record["wall_time_ns"] == str(WALL_NS)
  assert file_record["msg$s"] == "clock-independent record"


def test_standard_logging_preserves_existing_factory_and_creation_time(monkeypatch):
  monkeypatch.setattr(timing, "get_boot_id", lambda: BOOT_ID)
  monkeypatch.setattr(timing, "time", SimpleNamespace(time_ns=lambda: WALL_NS, monotonic_ns=lambda: MONO_NS))
  def existing_factory(*args, **kwargs):
    record = logging.LogRecord(*args, **kwargs)
    record.custom_attribute = "preserved"
    return record
  logging.setLogRecordFactory(existing_factory)
  swag = SwagLogger()
  installed = logging.getLogRecordFactory()
  SwagLogger()
  assert logging.getLogRecordFactory() is installed
  standard = logging.Logger("forwarded-logger")
  record = standard.makeRecord("forwarded-logger", logging.INFO, __file__, 1, "forwarded", (), None)
  monkeypatch.setattr(timing, "time", SimpleNamespace(time_ns=lambda: 1, monotonic_ns=lambda: 1))
  assert record.custom_attribute == "preserved"
  assert SwagFormatter(swag).format_dict(record)["monotonic_ns"] == str(MONO_NS)
  assert SwagFormatter(swag).format_dict(record)["wall_time_ns"] == str(WALL_NS)


@pytest.fixture
def gps_setup(monkeypatch):
  monkeypatch.setattr(timing, "get_boot_id", lambda: BOOT_ID)
  monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic_ns=lambda: MONO_NS))
  monkeypatch.setattr(timing, "min_date", lambda: datetime.datetime(2025, 2, 21))
  return SimpleNamespace(hasFix=True, unixTimestampMillis=1_790_394_870_545)


def test_gps_anchor_preserves_exact_sample_time(gps_setup):
  sample_ns = MONO_NS - 1_123_456_789
  anchor = timing.gps_clock_anchor(gps_setup, sample_ns)
  assert anchor == {
    "version": 1, "source": "gps", "clock": "monotonic", "boot_id": BOOT_ID,
    "monotonic_ns": str(sample_ns), "wall_time_ns": "1790394870545000000",
  }


@pytest.mark.parametrize("age_ns, accepted", [(0, True), (2_000_000_000, True), (2_000_000_001, False), (-1, False)])
def test_gps_anchor_freshness_boundary(gps_setup, age_ns, accepted):
  assert (timing.gps_clock_anchor(gps_setup, MONO_NS - age_ns) is not None) == accepted


@pytest.mark.parametrize("unix_ms", [0, -1, 10**100, 2_100_000_000_000, 1_790_394_870_545.5, True])
def test_gps_anchor_rejects_invalid_utc(gps_setup, unix_ms):
  gps_setup.unixTimestampMillis = unix_ms
  assert timing.gps_clock_anchor(gps_setup, MONO_NS) is None


def test_gps_anchor_requires_fix_and_kernel_identity(gps_setup, monkeypatch):
  gps_setup.hasFix = False
  assert timing.gps_clock_anchor(gps_setup, MONO_NS) is None
  gps_setup.hasFix = True
  monkeypatch.setattr(timing, "get_boot_id", lambda: None)
  assert timing.gps_clock_anchor(gps_setup, MONO_NS) is None


def test_clock_jump_is_evidence_not_an_anchor(gps_setup):
  assert timing.clock_jump(1_785_251_082_083_307_450, 1_790_394_822_440_324_620, MONO_NS) == {
    "version": 1, "clock": "monotonic", "boot_id": BOOT_ID, "monotonic_ns": str(MONO_NS),
    "old_offset_ns": "1785251082083307450", "new_offset_ns": "1790394822440324620",
  }
