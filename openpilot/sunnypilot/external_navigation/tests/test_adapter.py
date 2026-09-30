import ast
import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from openpilot.sunnypilot.external_navigation.config import load_config, validation_records
from openpilot.sunnypilot.external_navigation.model_adapter import is_validated, load_with_identity, car_identity, implementation_digest
from openpilot.sunnypilot.external_navigation.policy import PROFILE


class AdapterTests(unittest.TestCase):
  def test_consumed_bytes_identity(self):
    data = b'hello world' * 100
    def loader(source):
      first = source.read(5)
      buffer = bytearray(len(data) - 5)
      source.readinto(buffer)
      return first + buffer
    result, digest = load_with_identity(loader, io.BytesIO(data))
    self.assertEqual(result, data)
    self.assertEqual(digest, hashlib.sha256(data).hexdigest())

  def test_actual_oob_loaders_with_pickle_buffers(self):
    import pickle
    import struct
    from openpilot.sunnypilot.modeld_v2.helpers import load_oob as tinygrad_loader
    buffers = []
    data = b'weights' * 1024
    opcodes = pickle.dumps({'weights': pickle.PickleBuffer(bytearray(data))}, protocol=5, buffer_callback=buffers.append)
    payload = struct.pack('<q', len(opcodes)) + opcodes
    for buffer in buffers:
      value = buffer.raw()
      payload += struct.pack('<q', len(value)) + value
    source = (Path(__file__).resolve().parents[3] / 'selfdrive/modeld/helpers.py').read_text()
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'load_oob')
    namespace = {'io': io, 'pickle': pickle, 'struct': struct}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<stock load_oob>', 'exec'), namespace)
    for loader in (namespace['load_oob'], tinygrad_loader):
      model, digest = load_with_identity(loader, io.BytesIO(payload))
      self.assertEqual(bytes(model['weights']), data)
      self.assertEqual(digest, hashlib.sha256(payload).hexdigest())

  def test_exact_validation_gate(self):
    expected = {'car_sha256': 'car', 'model_sha256': 'model', 'pipeline': 'stock', 'code_sha256': 'code'}
    record = {'car_sha256': 'car', 'model_sha256': 'model', 'pipeline': 'stock', 'implementation_sha256': 'code',
              'profile': PROFILE, 'closed_course_passed': True}
    self.assertFalse(is_validated([], **expected))
    self.assertTrue(is_validated([record], **expected))
    for key in expected:
      changed = {**expected, key: 'changed'}
      self.assertFalse(is_validated([record], **changed))
    for key in ('profile', 'closed_course_passed'):
      self.assertFalse(is_validated([{**record, key: None}], **expected))
    self.assertFalse(is_validated([record], **{**expected, 'model_sha256': ''}))

  def test_missing_identity_evidence_fails_closed(self):
    from types import SimpleNamespace
    from unittest.mock import patch
    self.assertEqual(car_identity(SimpleNamespace()), '')
    self.assertEqual(car_identity(SimpleNamespace(to_bytes=lambda: b'car')), hashlib.sha256(b'car').hexdigest())
    with patch('pathlib.Path.read_bytes', side_effect=OSError):
      self.assertEqual(implementation_digest(), '')
    self.assertFalse(is_validated([{'closed_course_passed': True, 'car_sha256': '', 'model_sha256': '',
                                    'implementation_sha256': '', 'pipeline': 'stock', 'profile': PROFILE}],
                                 car_sha256='', model_sha256='', code_sha256='', pipeline='stock'))

  def test_private_configuration(self):
    import json
    with tempfile.TemporaryDirectory() as temporary:
      path = Path(temporary) / 'config.json'
      path.write_text(json.dumps({'relay_id': '01' * 16, 'key': '02' * 32}))
      path.chmod(0o600)
      self.assertEqual(load_config(path), (b'\x01' * 16, b'\x02' * 32))
      path.chmod(0o644)
      with self.assertRaises(ValueError):
        load_config(path)
      self.assertEqual(validation_records(path), [])
      path.chmod(0o600)
      path.write_text(json.dumps({'relay_id': '01' * 16, 'key': bytes(range(32)).hex()}))
      with self.assertRaises(ValueError):
        load_config(path)

  def test_both_pipeline_arbitration_and_health_isolation(self):
    repo = Path(__file__).resolve().parents[3]
    for relative in ('selfdrive/modeld/modeld.py', 'sunnypilot/modeld_v2/modeld.py'):
      source = (repo / relative).read_text()
      tree = ast.parse(source)
      self.assertIn('desire = navigation_hints.update(sm, DH, model)', source)
      self.assertIn('navigation_hints.fill(mdv2sp_send.modelDataV2SP)', source)
      self.assertIn('self.navigation_model_sha256 = load_with_identity', source)
      for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'SubMaster':
          self.assertNotIn('externalNavigationSP', ast.unparse(node))


class RuntimeAdapterTests(unittest.TestCase):
  def make_adapter(self):
    from types import SimpleNamespace
    from openpilot.sunnypilot.external_navigation.model_adapter import ExternalNavigationHints
    from openpilot.sunnypilot.external_navigation.policy import TurnPolicy
    nav = SimpleNamespace(receiveMonoTime=10_000_000_000, publisherSession=b'x' * 16, cacheEpoch=1,
                          streamId=2, generation=3, token=1, maneuverId=42, routeState=1, maneuverType=2,
                          hasNextDistance=True, nextDistance=30, distanceObservation=1, distanceAgeMs=0,
                          connected=True, available=True, hasManeuverId=True, maneuverObservation=1)
    class Subscriber(dict):
      valid = {'externalNavigationSP': True, 'carState': True, 'carControl': True}
      seen = {'externalNavigationSP': True}
      alive = {'carState': True, 'carControl': True}
      def update(self, timeout):
        pass
    hints = ExternalNavigationHints.__new__(ExternalNavigationHints)
    hints.sm = Subscriber(externalNavigationSP=nav)
    hints.params = SimpleNamespace(get=lambda key: '1')
    hints.policy = TurnPolicy()
    hints.pipeline, hints.car_sha256, hints.code_sha256 = 'stock', 'car', 'code'
    hints.records = [{'car_sha256': 'car', 'model_sha256': 'model', 'pipeline': 'stock',
                      'implementation_sha256': 'code', 'profile': PROFILE, 'closed_course_passed': True}]
    hints.mode, hints.next_parameter_read, hints.last_model, hints.last_transport = 0, 0, None, None
    car = SimpleNamespace(steeringPressed=False, brakePressed=False, leftBlinker=False, rightBlinker=False,
                          leftBlindspot=False, rightBlindspot=False, vEgo=4.)
    sm = Subscriber(carState=car, carControl=SimpleNamespace(latActive=True))
    dh = SimpleNamespace(lane_change_state=0, desire=0, lane_turn_controller=SimpleNamespace(lane_turn_value=8.))
    model = SimpleNamespace(navigation_model_sha256='model')
    return hints, nav, car, sm, dh, model

  def test_optional_subscriber_and_manual_arbitration(self):
    from unittest.mock import patch
    with patch('time.monotonic', return_value=10.), patch('time.monotonic_ns', return_value=10_000_000_000), \
         patch('openpilot.sunnypilot.external_navigation.model_adapter.read_mode', return_value=1):
      for control in ('steeringPressed', 'brakePressed', 'leftBlinker', 'rightBlinker', 'leftBlindspot', 'rightBlindspot'):
        hints, nav, car, sm, dh, model = self.make_adapter()
        self.assertEqual(hints.update(sm, dh, model), 0)
        setattr(car, control, True)
        nav.nextDistance, nav.distanceObservation = 10, 2
        self.assertEqual(hints.update(sm, dh, model), 0)
      hints, nav, car, sm, dh, model = self.make_adapter()
      hints.update(sm, dh, model)
      nav.nextDistance, nav.distanceObservation = 10, 2
      self.assertEqual(hints.update(sm, dh, model), 2)
      dh.desire = 3  # An existing driver-authorized lane change wins unchanged.
      self.assertEqual(hints.update(sm, dh, model), 3)

  def test_invalid_future_stale_and_changed_model(self):
    from unittest.mock import patch
    with patch('time.monotonic', return_value=10.), patch('time.monotonic_ns', return_value=10_000_000_000), \
         patch('openpilot.sunnypilot.external_navigation.model_adapter.read_mode', return_value=1):
      for invalid in ('missing', 'future', 'stale', 'model', 'validation', 'car_unhealthy'):
        hints, nav, car, sm, dh, model = self.make_adapter()
        hints.update(sm, dh, model)
        nav.nextDistance, nav.distanceObservation = 10, 2
        if invalid == 'missing':
          hints.sm.valid = {**hints.sm.valid, 'externalNavigationSP': False}
        elif invalid == 'future':
          nav.receiveMonoTime += 1_000_000_000
        elif invalid == 'stale':
          nav.receiveMonoTime -= 1_001_000_000
        elif invalid == 'model':
          model.navigation_model_sha256 = 'different'
        elif invalid == 'validation':
          hints.records = []
        else:
          sm.alive = {**sm.alive, 'carControl': False}
        self.assertEqual(hints.update(sm, dh, model), 0, invalid)


if __name__ == '__main__':
  unittest.main()
