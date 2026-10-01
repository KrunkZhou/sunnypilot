"""RTZS file settings independent of the prebuilt native Params registry."""
import contextlib
import fcntl
import os
from pathlib import Path
import secrets
import stat
import tempfile

from openpilot.sunnypilot.external_navigation.config import navigation_root, ensure_private_directory, read_private_bytes


def revision_path() -> Path:
  if Path('/AGNOS').is_file():
    return Path('/dev/shm/rtzs-navigation-revision')
  root = navigation_root() / 'runtime'
  ensure_private_directory(root, create=True)
  return root / 'network-revision'


@contextlib.contextmanager
def _lock(*, create: bool):
  root = navigation_root()
  ensure_private_directory(root, create=create)
  flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | (os.O_CREAT if create else 0)
  fd = os.open(root / 'config.lock', flags, 0o600)
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
      raise ValueError('config_lock_permissions')
    fcntl.flock(fd, fcntl.LOCK_EX if create else fcntl.LOCK_SH | fcntl.LOCK_NB)
    yield root / 'config'
  finally:
    os.close(fd)


def read_mode() -> int:
  try:
    with _lock(create=False) as config:
      if read_private_bytes(config / 'schema_version', 16) not in (b'1', b'1\n'):
        return 0
      value = read_private_bytes(config / 'mode', 16)
      return 1 if value in (b'1', b'1\n') else 0
  except (OSError, ValueError, TypeError):
    return 0


def read_turn_speed_control() -> bool:
  """Read the saved preference; runtime eligibility is checked by the consumer."""
  try:
    with _lock(create=False) as config:
      if read_private_bytes(config / 'schema_version', 16) not in (b'1', b'1\n'):
        return False
      return read_private_bytes(config / 'turn_speed_control', 16) in (b'1', b'1\n')
  except (OSError, ValueError, TypeError):
    return False


def _write_field(path: Path, value: bytes) -> None:
  if path.is_symlink() or (path.exists() and not path.is_file()):
    raise ValueError('configuration_field_type')
  temporary = None
  try:
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    with os.fdopen(fd, 'wb') as output:
      output.write(value)
      output.flush()
      os.fsync(output.fileno())
    os.replace(temporary, path)
    temporary = None
    directory = os.open(path.parent, os.O_RDONLY)
    try:
      os.fsync(directory)
    finally:
      os.close(directory)
  finally:
    if temporary is not None:
      with contextlib.suppress(OSError):
        os.unlink(temporary)


def write_mode(value: int) -> bool:
  if type(value) is not int or value not in (0, 1):
    return False
  return _write_setting('mode', str(value).encode('ascii') + b'\n')


def write_turn_speed_control(value: bool) -> bool:
  if type(value) is not bool:
    return False
  return _write_setting('turn_speed_control', b'1\n' if value else b'0\n')


def _write_setting(name: str, value: bytes) -> bool:
  try:
    with _lock(create=True) as config:
      ensure_private_directory(config, create=True)
      schema = config / 'schema_version'
      if schema.exists() or schema.is_symlink():
        if read_private_bytes(schema, 16) not in (b'1', b'1\n'):
          return False
      else:
        _write_field(config / 'mode', b'0\n')
        _write_field(schema, b'1\n')
      _write_field(config / name, value)
    return True
  except (OSError, ValueError):
    return False


def _revision_file():
  # All processes open read-write so loss of write access is also visible to the
  # automatic hotspot worker. The fixed-size record uses no ever-growing log.
  fd = os.open(revision_path(), os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
  info = os.fstat(fd)
  if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
    os.close(fd)
    raise ValueError('revision_permissions')
  return fd


def read_network_revision() -> str | None:
  try:
    fd = _revision_file()
    try:
      fcntl.flock(fd, fcntl.LOCK_EX)
      size = os.fstat(fd).st_size
      if size == 0:
        os.write(fd, b'0' * 32)
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
      value = os.read(fd, 33).decode('ascii')
      return value if len(value) == 32 and all(c in '0123456789abcdef' for c in value) else None
    finally:
      os.close(fd)
  except (OSError, ValueError, UnicodeError):
    return None


def record_network_action() -> bool:
  try:
    fd = _revision_file()
    try:
      fcntl.flock(fd, fcntl.LOCK_EX)
      value = secrets.token_hex(16).encode('ascii')
      if os.write(fd, value) != 32:
        os.ftruncate(fd, 1)  # Invalid record disables automatic network ownership.
        return False
      os.ftruncate(fd, 32)
      os.fsync(fd)
      return True
    except OSError:
      with contextlib.suppress(OSError):
        os.ftruncate(fd, 1)
      return False
    finally:
      os.close(fd)
  except (OSError, ValueError):
    return False
