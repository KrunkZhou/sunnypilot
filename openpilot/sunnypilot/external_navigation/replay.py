"""Development tools. Reports never grant on-road model validation automatically."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path

from openpilot.sunnypilot.external_navigation.policy import Sample, TurnPolicy


def replay_policy(rows):
  policy = TurnPolicy()
  for row in rows:
    sample_data = row.get('sample')
    if sample_data is not None:
      sample_data = dict(sample_data)
      sample_data['identity'] = tuple(sample_data['identity'])
    decision = policy.update(Sample(**sample_data) if sample_data else None,
                             mode=1, verified=False, **row['vehicle'])
    yield {'timestamp': row.get('timestamp'), **asdict(decision)}


def compare_outputs(neutral, hinted):
  def index(rows):
    indexed = {}
    for row in rows:
      frame = row['frameId']
      if frame in indexed:
        raise ValueError('Duplicate model frame')
      indexed[frame] = row
    return indexed
  a, b = index(neutral), index(hinted)
  if not a or a.keys() != b.keys():
    raise ValueError('Replay frame identities do not match')
  report = {'frames': len(a), 'metrics': {}, 'activation_approved': False}
  for metric in ('positionY', 'velocityX', 'desiredAcceleration'):
    delta = []
    for frame in sorted(a):
      before, after = a[frame][metric], b[frame][metric]
      before = before if isinstance(before, list) else [before]
      after = after if isinstance(after, list) else [after]
      if len(before) != len(after) or not before:
        raise ValueError('Replay trajectory shapes do not match')
      if not all(math.isfinite(value) for value in before + after):
        raise ValueError('Nonfinite model output')
      delta.extend(y - x for x, y in zip(before, after, strict=True))
    report['metrics'][metric] = {'mean_delta': sum(delta) / len(delta), 'max_absolute_delta': max(map(abs, delta))}
  return report


def camera_replay(args):
  """Uses the existing process replay harness, in its isolated Params/IPC namespace."""
  import copy
  import os
  from openpilot.common.hardware import PC
  if not PC:
    raise RuntimeError('Camera comparison runs on a development PC, never an installed vehicle')
  from openpilot.selfdrive.test.process_replay.process_replay import get_process_config, replay_process
  from openpilot.system.manager.process import PythonProcess
  from openpilot.system.manager.process_config import managed_processes
  from openpilot.tools.lib.framereader import FrameReader
  from openpilot.tools.lib.logreader import LogReader
  logs = list(LogReader(str(args.log)))
  if not any(m.which() == 'narrowRoadCameraState' and m.narrowRoadCameraState.frameId == args.pulse_frame for m in logs):
    raise ValueError('Pulse frame not present in the recording')
  frames = {'narrowRoadCameraState': FrameReader(str(args.narrow), pix_fmt='nv12')}
  if args.wide:
    frames['wideRoadCameraState'] = FrameReader(str(args.wide), pix_fmt='nv12')
  cfg = copy.deepcopy(get_process_config('modeld'))
  previous_process = managed_processes['modeld']
  env_keys = ('EXTERNAL_NAV_REPLAY_FRAME', 'EXTERNAL_NAV_REPLAY_HINT', 'EXTERNAL_NAV_REPLAY_PIPELINE')
  previous_env = {key: os.environ.get(key) for key in env_keys}
  managed_processes['modeld'] = PythonProcess('modeld', 'openpilot.sunnypilot.external_navigation.replay_model', lambda *a: True)
  outputs = []
  try:
    os.environ['EXTERNAL_NAV_REPLAY_FRAME'] = str(args.pulse_frame)
    os.environ['EXTERNAL_NAV_REPLAY_PIPELINE'] = args.pipeline
    for hint in (0, {'turnLeft': 1, 'turnRight': 2, 'keepRight': 6}[args.hint]):
      os.environ['EXTERNAL_NAV_REPLAY_HINT'] = str(hint)
      result = replay_process(cfg, logs, frames)
      rows = [{'frameId': m.modelV2.frameId, 'positionY': list(m.modelV2.position.y),
               'velocityX': list(m.modelV2.velocity.x), 'desiredAcceleration': m.modelV2.action.desiredAcceleration}
              for m in result if m.which() == 'modelV2']
      outputs.append(rows)
  finally:
    managed_processes['modeld'] = previous_process
    for key, value in previous_env.items():
      if value is None:
        os.environ.pop(key, None)
      else:
        os.environ[key] = value
  args.output.mkdir(parents=True, exist_ok=True)
  for name, rows in zip(('neutral', 'hinted'), outputs, strict=True):
    (args.output / f'{name}.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
  report = compare_outputs(*outputs)
  report.update({'hint': args.hint, 'pulse_frame': args.pulse_frame, 'pipeline': args.pipeline})
  (args.output / 'comparison.json').write_text(json.dumps(report, indent=2) + '\n')
  return report


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  sub = parser.add_subparsers(dest='command', required=True)
  policy = sub.add_parser('policy', help='Replay recorded guidance + vehicle state; proposals only')
  policy.add_argument('trace', type=Path)
  compare = sub.add_parser('compare', help='Compare paired exported model outputs')
  compare.add_argument('neutral', type=Path)
  compare.add_argument('hinted', type=Path)
  camera = sub.add_parser('camera', help='Run neutral and hinted recorded-camera model replay on a development PC')
  camera.add_argument('--log', type=Path, required=True)
  camera.add_argument('--narrow', type=Path, required=True)
  camera.add_argument('--wide', type=Path)
  camera.add_argument('--pulse-frame', type=int, required=True)
  camera.add_argument('--pipeline', choices=('stock', 'tinygrad'), default='stock')
  camera.add_argument('--hint', choices=('turnLeft', 'turnRight', 'keepRight'), required=True)
  camera.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  def rows(path):
    with path.open() as stream:
      return [json.loads(line) for line in stream if line.strip()]
  if args.command == 'policy':
    for result in replay_policy(rows(args.trace)):
      print(json.dumps(result))
  elif args.command == 'compare':
    print(json.dumps(compare_outputs(rows(args.neutral), rows(args.hinted)), indent=2))
  else:
    print(json.dumps(camera_replay(args), indent=2))


if __name__ == '__main__':
  main()
