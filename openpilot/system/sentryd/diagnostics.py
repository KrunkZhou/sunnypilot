from __future__ import annotations

import math
from itertools import islice

from openpilot.common.swaglog import cloudlog


MAX_DIAGNOSTIC_TEXT = 256


def bounded_diagnostic(value: object, depth: int = 0) -> object:
  """Keep diagnostic records bounded and never serialize camera buffers."""
  if isinstance(value, str):
    return " ".join(value[:MAX_DIAGNOSTIC_TEXT].replace("\x00", " ").split())
  if value is None or isinstance(value, (bool, int)):
    return value
  if isinstance(value, float):
    return round(value, 3) if math.isfinite(value) else None
  if depth < 4 and isinstance(value, dict):
    return {str(key)[:64]: bounded_diagnostic(item, depth + 1) for key, item in islice(value.items(), 16)}
  # No repr() of unknown objects: VisionIPC frames and subprocess output can
  # contain captured image data. Callers explicitly supply exception text.
  return "<omitted>"


def log_capture_diagnostic(event: str, **fields: object) -> None:
  """Logging must not change capture outcomes or prevent lease cleanup."""
  try:
    cloudlog.event(event, **{key: bounded_diagnostic(value) for key, value in fields.items()})
  except Exception:
    pass
