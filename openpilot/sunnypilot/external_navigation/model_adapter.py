"""Optional model input arbitration. Disabled/missing navigation cannot fail model health checks."""
from __future__ import annotations

import hashlib
from pathlib import Path
import time

from openpilot.sunnypilot.external_navigation.config import validation_records
from openpilot.sunnypilot.external_navigation.settings import read_mode
from openpilot.sunnypilot.external_navigation.policy import PROFILE, Sample, TurnPolicy


class DigestReader:
  """Hash the exact bytes consumed by the existing model loader, without a second load."""
  def __init__(self, source):
    self.source = source
    self.digest = hashlib.sha256()

  def read(self, count=-1):
    data = self.source.read(count)
    self.digest.update(data)
    return data

  def readinto(self, target):
    count = self.source.readinto(target)
    if count:
      self.digest.update(memoryview(target).cast('B')[:count])
    return count


def load_with_identity(loader, source):
  with source:
    reader = DigestReader(source)
    model = loader(reader)
    return model, reader.digest.hexdigest()


def implementation_digest() -> str:
  try:
    digest = hashlib.sha256()
    source = Path(__file__).resolve()
    for name in ('policy.py', 'model_adapter.py', 'protocol.py', 'receiver.py', 'settings.py', 'config.py'):
      digest.update(source.with_name(name).read_bytes())
    for name in ('selfdrive/modeld/modeld.py', 'sunnypilot/modeld_v2/modeld.py'):
      digest.update((source.parents[2] / name).read_bytes())
    return digest.hexdigest()
  except OSError:
    return ''  # Missing optional source evidence disables hints, never the driving model.


def car_identity(CP) -> str:
  try:
    builder = CP.as_builder() if hasattr(CP, 'as_builder') else CP
    return hashlib.sha256(builder.to_bytes()).hexdigest()
  except Exception:
    return ''  # Demo/custom CarParams implementations may not expose serializable evidence.


def is_validated(records: list[dict], *, car_sha256: str, model_sha256: str, pipeline: str, code_sha256: str) -> bool:
  expected = {'car_sha256': car_sha256, 'model_sha256': model_sha256,
              'pipeline': pipeline, 'implementation_sha256': code_sha256, 'profile': PROFILE}
  return bool(car_sha256 and model_sha256 and code_sha256) and any(isinstance(record, dict) and record.get('closed_course_passed') is True
                                   and all(record.get(key) == value for key, value in expected.items()) for record in records)


class ExternalNavigationHints:
  def __init__(self, CP, pipeline: str):
    from openpilot.cereal.messaging import SubMaster
    self.sm = SubMaster(['externalNavigationSP'])  # Optional, never included in model/all_checks.
    self.policy = TurnPolicy()
    self.pipeline = pipeline
    self.car_sha256 = car_identity(CP)
    self.code_sha256 = implementation_digest()
    self.records = validation_records()  # Local closed-course evidence, none shipped; restart to reload.
    self.mode = 0
    self.next_parameter_read = 0.
    self.decision = self.policy.status
    self.last_model = None
    self.last_transport = None

  def update(self, sm, desire_helper, model):
    now = time.monotonic()
    self.sm.update(0)
    if now >= self.next_parameter_read:
      self.mode = read_mode()
      self.next_parameter_read = now + 1
    model_digest = getattr(model, 'navigation_model_sha256', '')
    if self.last_model != model_digest:
      self.policy._reset()  # Model switch must observe a new approach, never use an already armed turn.
      self.last_model = model_digest
    sample = None
    nav = self.sm['externalNavigationSP']
    if self.sm.valid['externalNavigationSP'] and self.sm.seen['externalNavigationSP']:
      transport = bytes(getattr(nav, 'receiverSession', b'')), bytes(nav.publisherSession), nav.token
      if transport != self.last_transport:
        self.policy._reset()  # A missed transient reset publication cannot carry an armed turn across BLE resync.
        self.last_transport = transport
      receive_age = (time.monotonic_ns() - nav.receiveMonoTime) // 1000000
      sample = Sample(
        (bytes(nav.publisherSession), nav.cacheEpoch, nav.streamId, nav.generation, nav.maneuverId),
        nav.routeState, nav.maneuverType, nav.nextDistance if nav.hasNextDistance else None,
        nav.distanceObservation, min(0xffffffff, nav.distanceAgeMs + max(0, receive_age)),
        nav.connected and 0 <= receive_age <= 1000, nav.available,
        nav.hasManeuverId and nav.distanceObservation > 0 and nav.maneuverObservation > 0,
      )
    cs = sm['carState']
    manual = bool(cs.steeringPressed or cs.brakePressed or cs.leftBlinker or cs.rightBlinker
                  or cs.leftBlindspot or cs.rightBlindspot or int(desire_helper.lane_change_state) != 0
                  or int(desire_helper.desire) != 0)
    verified = is_validated(self.records, car_sha256=self.car_sha256, model_sha256=model_digest,
                            pipeline=self.pipeline, code_sha256=self.code_sha256)
    self.decision = self.policy.update(
      sample, mode=self.mode, verified=verified,
      lateral_active=sm['carControl'].latActive and sm.valid['carState'] and sm.valid['carControl']
                     and sm.alive['carState'] and sm.alive['carControl'],
      speed=cs.vEgo, speed_limit=desire_helper.lane_turn_controller.lane_turn_value, manual=manual)
    # Existing desires always win. The policy returns only none/turnLeft/turnRight.
    return desire_helper.desire if int(desire_helper.desire) != 0 else self.decision.desire

  def fill(self, message) -> None:
    message.navigationHint = self.decision.proposal
    message.navigationHintStatus = self.decision.reason
    message.navigationAssisted = self.decision.assisted
