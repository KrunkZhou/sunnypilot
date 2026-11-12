import ast
import io
from pathlib import Path
import tempfile
import unittest

from openpilot.sunnypilot.external_navigation.config import load_config


class AdapterTests(unittest.TestCase):
  def test_stock_path_oob_loader_and_device_context(self):
    from contextlib import contextmanager
    import pathlib
    import pickle
    import struct
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    data = b'weights' * 1024
    marker, stream = object(), io.BytesIO()
    pickler = pickle.Pickler(stream, protocol=5)
    pickler.persistent_id = lambda value: (len(data), 0) if value is marker else None
    pickler.dump({'weights': marker})
    opcodes = stream.getvalue()
    opened, contexts = [], []

    def tracked_open(*args, **kwargs):
      file = open(*args, **kwargs)
      opened.append(file)
      return file

    @contextmanager
    def context(**settings):
      contexts.append(settings)
      try:
        yield
      finally:
        contexts.pop()

    def view(size, offset):
      self.assertTrue(opened[-1].closed)
      self.assertEqual(len(contexts), 1)
      return memoryview(data)[offset:offset + size]

    # Execute both real loader functions, isolating only the native Tensor/device boundary.
    tensor = MagicMock()
    tensor.return_value.__getitem__.return_value.to.return_value.realize.return_value.uop.buffer = SimpleNamespace(view=view)
    device = SimpleNamespace(DEFAULT='test-device')
    namespace = {'io': io, 'pickle': pickle, 'struct': struct, 'pathlib': pathlib, 'Tensor': tensor,
                 'Device': device, 'open': tracked_open, 'sys': SimpleNamespace(platform='darwin'), 'AGNOS': False}
    repo = Path(__file__).resolve().parents[4]
    for relative, name in (('tinygrad_repo/examples/openpilot/helpers.py', 'load_pickle'),
                           ('openpilot/selfdrive/modeld/helpers.py', 'load_oob')):
      tree = ast.parse((repo / relative).read_text())
      function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
      exec(compile(ast.Module(body=[function], type_ignores=[]), relative, 'exec'), namespace)

    def load_pickle(path, **kwargs):
      self.assertEqual(contexts, [{'DEV': expected_device}])
      return namespace['load_pickle'](path, **kwargs)

    modules = {'tinygrad': SimpleNamespace(Context=context),
               'tinygrad_repo.examples.openpilot.helpers': SimpleNamespace(load_pickle=load_pickle)}
    with tempfile.TemporaryDirectory() as temporary, patch.dict('sys.modules', modules):
      path = Path(temporary) / 'model.pkl'
      path.write_bytes(struct.pack('<q', len(opcodes)) + opcodes + data)
      for chestnut, agnos, platform, expected_device in ((True, True, 'linux', 'USB+AMD:LLVM'),
                                                       (False, True, 'linux', 'QCOM'),
                                                       (False, False, 'darwin', 'METAL'),
                                                       (False, False, 'linux', 'CPU:LLVM')):
        with self.subTest(device=expected_device):
          namespace['AGNOS'], namespace['sys'].platform = agnos, platform
          model = namespace['load_oob'](path, chestnut)
          tensor.assert_called_with(path)
          tensor.return_value.__getitem__.assert_called_with(slice(8 + len(opcodes), None))
          tensor.return_value.__getitem__.return_value.to.assert_called_with(device.DEFAULT)
          self.assertEqual(contexts, [])
          self.assertEqual(bytes(model['weights']), data)
    self.assertTrue(all(file.closed for file in opened))
    self.assertEqual(bytes(model['weights']), data)

  def test_plain_bundle_loader_preserves_extension_and_mapped_data(self):
    from unittest.mock import Mock, patch
    from openpilot.sunnypilot.modeld_v2 import helpers

    data, added = b'weights' * 1024, b'warp'
    extras = {'metadata': {'input_shapes': {'image': (1, 3, 8, 8)}}}
    opened = []

    def tracked_open(*args, **kwargs):
      file = open(*args, **kwargs)
      opened.append(file)
      return file

    def extend(parts, metadata):
      self.assertEqual(set(parts), {'vision'})
      self.assertEqual(metadata, extras)
      header, blob = parts['vision']
      self.assertEqual(bytes(blob[slice(*header['data'])]), data)
      return {'warp': ({'data': [0, len(added)], 'calls': []}, memoryview(added))}

    def compile_part(header, blob, device_map, verify, shared):
      self.assertTrue(all(file.closed for file in opened))
      return blob[slice(*header['data'])]

    extension = Mock(side_effect=extend)
    with tempfile.TemporaryDirectory() as temporary:
      path = Path(temporary) / 'model.pkl'
      helpers.write_bundle(path, {'vision': ({'data': [0, len(data)], 'calls': []}, data)}, extras)
      # Parse the real bundle and run its extension callback; native compilation is out of scope.
      with patch.object(helpers, 'open', tracked_open, create=True), patch.object(helpers, 'PlainJit', side_effect=compile_part) as jit:
        model = helpers.load_model(path, device_map={'QCOM': 'CPU'}, verify=True, cache=False, extend=extension)
      extension.assert_called_once()
      self.assertEqual(jit.call_count, 2)
      self.assertEqual(jit.call_args_list[0].args[2:4], ({'QCOM': 'CPU'}, True))
      self.assertIs(jit.call_args_list[0].args[4], jit.call_args_list[1].args[4])
    self.assertEqual(model['metadata'], extras['metadata'])
    self.assertEqual(bytes(model['vision']), data)
    self.assertEqual(bytes(model['warp']), added)
    self.assertTrue(opened and all(file.closed for file in opened))

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
      self.assertIn('navigation_hints = ExternalNavigationHints()', source)
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
    hints.mode, hints.next_parameter_read, hints.last_model, hints.last_transport = 0, 0, None, None
    hints.turn_event = hints.turn_event_time = 0
    car = SimpleNamespace(steeringPressed=False, brakePressed=False, leftBlinker=False, rightBlinker=False,
                          leftBlindspot=False, rightBlindspot=False, vEgo=4.)
    sm = Subscriber(carState=car, carControl=SimpleNamespace(latActive=True))
    dh = SimpleNamespace(lane_change_state=0, desire=0, lane_turn_controller=SimpleNamespace(lane_turn_value=8.))
    model = object()  # No vehicle/model identity or approval evidence exists.
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

  def test_invalid_future_stale_and_changed_model_instance(self):
    from unittest.mock import patch
    with patch('time.monotonic', return_value=10.), patch('time.monotonic_ns', return_value=10_000_000_000), \
         patch('openpilot.sunnypilot.external_navigation.model_adapter.read_mode', return_value=1):
      for invalid in ('missing', 'future', 'stale', 'model', 'car_unhealthy'):
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
          model = object()
        else:
          sm.alive = {**sm.alive, 'carControl': False}
        self.assertEqual(hints.update(sm, dh, model), 0, invalid)

  def test_turn_without_vehicle_model_identity_or_approval_file(self):
    from unittest.mock import patch
    with patch('time.monotonic', return_value=10.), patch('time.monotonic_ns', return_value=10_000_000_000), \
         patch('openpilot.sunnypilot.external_navigation.model_adapter.read_mode', return_value=1):
      hints, nav, car, sm, dh, model = self.make_adapter()
      self.assertEqual(hints.update(sm, dh, model), 0)
      nav.nextDistance, nav.distanceObservation = 10, 2
      self.assertEqual(hints.update(sm, dh, model), 2)
      self.assertTrue(hints.decision.assisted)
      self.assertEqual(hints.decision.reason, 'assisted_turn_proposed')

  def test_replacement_model_can_use_a_new_approach(self):
    from unittest.mock import patch
    with patch('time.monotonic', return_value=10.), patch('time.monotonic_ns', return_value=10_000_000_000), \
         patch('openpilot.sunnypilot.external_navigation.model_adapter.read_mode', return_value=1):
      hints, nav, car, sm, dh, model = self.make_adapter()
      hints.update(sm, dh, model)
      model = object()
      nav.nextDistance, nav.distanceObservation = 10, 2
      self.assertEqual(hints.update(sm, dh, model), 0)
      nav.nextDistance, nav.distanceObservation = 30, 3
      self.assertEqual(hints.update(sm, dh, model), 0)
      nav.nextDistance, nav.distanceObservation = 20, 4
      self.assertEqual(hints.update(sm, dh, model), 0)
      nav.nextDistance, nav.distanceObservation = 10, 5
      self.assertEqual(hints.update(sm, dh, model), 2)

  def test_turn_event_requires_confirmed_effective_pulse_and_is_latched(self):
    from types import SimpleNamespace
    from unittest.mock import patch
    with patch('time.monotonic', return_value=10.), patch('time.monotonic_ns', return_value=10_000_000_000), \
         patch('openpilot.sunnypilot.external_navigation.model_adapter.read_mode', return_value=1):
      hints, nav, car, sm, dh, model = self.make_adapter()
      hints.update(sm, dh, model)
      hints.model_completed(2, [0, 0, 1])  # Eligibility alone is not a pulse.
      self.assertEqual(hints.turn_event_time, 0)
      nav.nextDistance, nav.distanceObservation = 10, 2
      self.assertEqual(hints.update(sm, dh, model), 2)
      message = SimpleNamespace()
      hints.fill(message)  # Inference not completed: proposal is logged, notice stays absent.
      self.assertEqual(message.navigationHint, 2)
      self.assertEqual(message.navigationTurnEventMonoTime, 0)
      hints.model_completed(2, [0, 0, 0])  # Model edge filter suppressed a repeated desire.
      hints.model_completed(3, [0, 0, 0, 1])  # Existing lane-change desire is never a nav event.
      self.assertEqual(hints.turn_event_time, 0)
      hints.model_completed(2, [0, 0, 1])
      hints.fill(message)
      self.assertEqual((message.navigationTurnEvent, message.navigationTurnEventMonoTime), (2, 10_000_000_000))
      with patch('time.monotonic_ns', return_value=10_200_000_000):
        hints.update(sm, dh, model)
        hints.model_completed(0, [0, 0, 0])
        hints.fill(message)
      self.assertEqual(message.navigationHint, 0)
      self.assertEqual((message.navigationTurnEvent, message.navigationTurnEventMonoTime), (2, 10_000_000_000))

  def test_input_age_diagnostics_explain_stale_reset_without_extending_it(self):
    from types import SimpleNamespace
    from unittest.mock import patch
    with patch('time.monotonic', return_value=10.), patch('time.monotonic_ns', return_value=10_000_000_000), \
         patch('openpilot.sunnypilot.external_navigation.model_adapter.read_mode', return_value=1):
      hints, nav, car, sm, dh, model = self.make_adapter()
      nav.distanceAgeMs = 800
      hints.update(sm, dh, model)
      self.assertEqual(hints.decision.reason, 'assisted_armed')
      with patch('time.monotonic_ns', return_value=10_201_000_000):
        hints.update(sm, dh, model)
        message = SimpleNamespace()
        hints.fill(message)
        self.assertEqual(hints.decision.reason, 'distance_stale_or_missing')
        self.assertEqual(message.navigationInputDistanceAgeMs, 1001)
        self.assertEqual(message.navigationInputReceiveAgeMs, 201)
        self.assertEqual(message.navigationInputMonoTime, 10_201_000_000)
        self.assertEqual(message.navigationDistanceObservation, 1)
        self.assertEqual(message.navigationTransportToken, 1)
        nav.receiveMonoTime, nav.distanceAgeMs = 10_201_000_000, 0
        nav.nextDistance, nav.distanceObservation = 10, 2
        self.assertEqual(hints.update(sm, dh, model), 0)
        self.assertEqual(hints.decision.reason, 'assisted_waiting_for_approach')

  def test_completed_hook_is_only_on_successful_output_in_both_pipelines(self):
    root = Path(__file__).resolve().parents[3]
    for path in ('selfdrive/modeld/modeld.py', 'sunnypilot/modeld_v2/modeld.py'):
      tree = ast.parse((root / path).read_text())
      matches = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                 and ast.unparse(node.test) == 'model_output is not None']
      self.assertEqual(len(matches), 1)
      block = matches[0]
      self.assertIsInstance(block.body[0], ast.Expr)
      self.assertIn('navigation_hints.model_completed(desire, model.', ast.unparse(block.body[0]))
      self.assertLess(ast.unparse(block).index('model_completed'), ast.unparse(block).index('navigation_hints.fill'))


if __name__ == '__main__':
  unittest.main()
