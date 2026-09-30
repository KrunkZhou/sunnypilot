"""Private RTZS navigation provisioning; never Params, Cereal or uploaded diagnostics."""
import json
import os
from pathlib import Path
import stat


def navigation_root() -> Path:
  if override := os.environ.get('RTZS_NAVIGATION_ROOT'):
    return Path(override)
  if Path('/AGNOS').is_file():
    return Path('/data/rtzs/navigation')
  return Path.home() / ('.comma' + os.environ.get('OPENPILOT_PREFIX', '')) / 'rtzs/navigation'


def ensure_private_directory(path: Path, *, create: bool = False) -> None:
  if create:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
  info = path.lstat()
  if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
    raise ValueError('private_directory_permissions')


def read_private_bytes(path: Path, maximum: int) -> bytes:
  ensure_private_directory(path.parent)
  fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid() or info.st_size > maximum:
      raise ValueError('private_configuration_permissions')
    value = os.read(fd, maximum + 1)
    if len(value) > maximum:
      raise ValueError('private_configuration_length')
    return value
  finally:
    os.close(fd)


def read_private_json(path: Path) -> dict:
  value = json.loads(read_private_bytes(path, 16384))
  if not isinstance(value, dict):
    raise ValueError('private_configuration_shape')
  return value


def load_config(path: Path | None = None) -> tuple[bytes, bytes]:
  value = read_private_json(path or navigation_root() / 'private/relay.json')
  relay, key = bytes.fromhex(value['relay_id']), bytes.fromhex(value['key'])
  if len(relay) != 16 or len(key) != 32 or relay == bytes(16) or key == bytes(32):
    raise ValueError('private_configuration_identity')
  if key == bytes(range(32)):
    raise ValueError('public_test_key')
  return relay, key
