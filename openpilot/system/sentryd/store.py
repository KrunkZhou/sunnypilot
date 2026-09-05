from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from openpilot.system.sentryd.config import default_sentry_root


CAMERA_ROLES = ("wide", "cabin")
OMISSION_REASONS = frozenset((
  "queue_quota",
  "capture_failed",
  "camera_unavailable",
  "capture_timeout",
  "ignition_on",
  "stale_capture",
))
MAX_MEDIA_BYTES = 8 * 1024 * 1024
DEFAULT_MEDIA_QUOTA = 1024 * 1024 * 1024
UPLOAD_CLAIM_SECONDS = 120.0
MAX_REVISION = 2 ** 31 - 1


@dataclass(frozen=True)
class MediaData:
  data: bytes
  width: int
  height: int


@dataclass(frozen=True)
class QueuedMedia:
  role: str
  path: str
  sha256: str
  size: int
  width: int
  height: int


@dataclass(frozen=True)
class QueuedRevision:
  event_id: str
  revision: int
  metadata: dict[str, Any]
  media: tuple[QueuedMedia, ...]
  attempts: int


@dataclass(frozen=True)
class OutboxStats:
  pending: int
  terminal: int
  media_bytes: int


def default_store_path() -> Path:
  return default_sentry_root() / "outbox.sqlite3"


def read_outbox_stats(path: str | Path | None = None) -> OutboxStats:
  database = Path(path) if path is not None else default_store_path()
  try:
    parent_info = database.parent.lstat()
  except OSError as exc:
    raise OSError(f"unsafe Sentry outbox directory: {database.parent}") from exc
  if (stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode) or
      stat.S_IMODE(parent_info.st_mode) != 0o700):
    raise OSError(f"unsafe Sentry outbox directory: {database.parent}")
  try:
    before = database.lstat()
  except FileNotFoundError:
    return OutboxStats(0, 0, 0)
  if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or
      stat.S_IMODE(before.st_mode) != 0o600):
    raise OSError(f"unsafe Sentry outbox database: {database}")
  sidecars: dict[str, tuple[int, int]] = {}
  for suffix in ("-wal", "-shm", "-journal"):
    sidecar = Path(str(database) + suffix)
    try:
      info = sidecar.lstat()
    except FileNotFoundError:
      continue
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
        stat.S_IMODE(info.st_mode) != 0o600):
      raise OSError(f"unsafe Sentry outbox sidecar: {sidecar}")
    sidecars[suffix] = (info.st_dev, info.st_ino)
  connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1)
  connection.row_factory = sqlite3.Row
  try:
    after = database.lstat()
    if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or
        stat.S_ISLNK(after.st_mode) or not stat.S_ISREG(after.st_mode) or stat.S_IMODE(after.st_mode) != 0o600):
      raise OSError(f"Sentry outbox database changed while opening: {database}")
    for suffix in ("-wal", "-shm", "-journal"):
      sidecar = Path(str(database) + suffix)
      try:
        info = sidecar.lstat()
      except FileNotFoundError:
        continue
      if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or
          stat.S_IMODE(info.st_mode) != 0o600):
        raise OSError(f"unsafe Sentry outbox sidecar: {sidecar}")
      previous = sidecars.get(suffix)
      if previous is not None and previous != (info.st_dev, info.st_ino):
        raise OSError(f"Sentry outbox sidecar changed while opening: {sidecar}")
    row = connection.execute(
      """
      SELECT
        (SELECT COUNT(*) FROM revisions WHERE state IN (
          'capturing', 'ready', 'uploading', 'evicting_ready', 'evicting_terminal', 'evicting_uncertain'
        )) AS pending,
        (SELECT COUNT(*) FROM revisions WHERE state='terminal') AS terminal,
        COALESCE((SELECT SUM(size) FROM media WHERE path IS NOT NULL), 0) AS media_bytes
      """
    ).fetchone()
    return OutboxStats(row["pending"], row["terminal"], row["media_bytes"])
  finally:
    connection.close()


class SentryStore:
  def __init__(self, path: str | Path | None = None, *, media_quota_bytes: int = DEFAULT_MEDIA_QUOTA,
               run_maintenance: bool = True):
    self.path = Path(path) if path is not None else default_store_path()
    self.root = self.path.parent
    self.media_root = self.root / "media"
    self.media_quota_bytes = media_quota_bytes
    self._ensure_private_directory(self.root)
    self._ensure_private_directory(self.media_root)
    self._sidecar_inodes: dict[str, tuple[int, int]] = {}
    for suffix in ("-wal", "-shm", "-journal"):
      sidecar = Path(str(self.path) + suffix)
      if sidecar.exists() or sidecar.is_symlink():
        info = sidecar.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
          raise OSError(f"unsafe Sentry outbox sidecar: {sidecar}")
        self._sidecar_inodes[suffix] = (info.st_dev, info.st_ino)
    before = None
    if self.path.exists() or self.path.is_symlink():
      before = self.path.lstat()
      if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or
          stat.S_IMODE(before.st_mode) != 0o600):
        raise OSError(f"unsafe Sentry outbox database: {self.path}")
    else:
      flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
      if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
      if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
      try:
        fd = os.open(self.path, flags, 0o600)
      except FileExistsError:
        # Another opener can create the private database before SQLite takes
        # the schema lock. Validate that inode just like a pre-existing file.
        pass
      else:
        try:
          os.fchmod(fd, 0o600)
          os.fsync(fd)
        finally:
          os.close(fd)
        self._fsync_directory(self.root)
      before = self.path.lstat()
      if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or
          stat.S_IMODE(before.st_mode) != 0o600):
        raise OSError(f"unsafe Sentry outbox database: {self.path}")
    self.connection = sqlite3.connect(self.path, timeout=10)
    after = self.path.lstat()
    if (stat.S_ISLNK(after.st_mode) or not stat.S_ISREG(after.st_mode) or stat.S_IMODE(after.st_mode) != 0o600 or
        (before is not None and (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino))):
      self.connection.close()
      raise OSError(f"Sentry outbox database changed while opening: {self.path}")
    try:
      self.connection.row_factory = sqlite3.Row
      self.connection.execute("PRAGMA foreign_keys=ON")
      self._enable_wal()
      self.connection.execute("PRAGMA synchronous=FULL")
      synchronous = self.connection.execute("PRAGMA synchronous").fetchone()[0]
      if synchronous != 2:
        raise OSError(f"Sentry outbox could not enable FULL synchronous mode: {synchronous}")
      self._create_schema()
      self._secure_sidecars()
    except Exception:
      self.connection.close()
      raise
    if run_maintenance:
      try:
        self.recover_expired_upload_claims()
        self.recover_interrupted_evictions()
        self.cleanup_acknowledged_media()
        self.recover_interrupted_captures()
        self.reconcile_media_tree()
        self.enforce_media_quota()
      except Exception:
        self.connection.close()
        raise

  def close(self) -> None:
    self.connection.close()

  def _enable_wal(self) -> None:
    # journal_mode can report SQLITE_BUSY immediately during the first WAL
    # conversion despite the connection's busy timeout. A bounded retry lets
    # concurrent first-time initializers converge without masking I/O errors.
    deadline = time.monotonic() + 10.0
    while True:
      try:
        journal_mode = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        break
      except sqlite3.OperationalError as exc:
        code = getattr(exc, "sqlite_errorcode", 0) & 0xff
        remaining = deadline - time.monotonic()
        if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or remaining <= 0:
          raise
        time.sleep(min(0.05, remaining))
    if str(journal_mode).lower() != "wal":
      raise OSError(f"Sentry outbox could not enable WAL mode: {journal_mode}")

  def begin_revision(self, *, event_id: str, revision: int, kind: str, source: str,
                     episode_started_at: str, detected_at: str, message: str, schema_version: int = 1) -> None:
    self._validate_revision(event_id, revision, kind, source, episode_started_at, detected_at, message, schema_version)
    detected_at = self._timestamp_at_least(detected_at, episode_started_at)
    self.connection.execute("BEGIN IMMEDIATE")
    with self.connection:
      existing_event = self.connection.execute(
        "SELECT source, episode_started_at, schema_version FROM events WHERE event_id=?", (event_id,)
      ).fetchone()
      if existing_event is None:
        if revision != 1:
          raise ValueError("an event must begin with revision 1")
        self.connection.execute(
          "INSERT INTO events(event_id, source, episode_started_at, schema_version) VALUES (?, ?, ?, ?)",
          (event_id, source, episode_started_at, schema_version),
        )
      elif (existing_event["source"], existing_event["episode_started_at"], existing_event["schema_version"]) != (
          source, episode_started_at, schema_version):
        raise ValueError("event identity fields cannot change between revisions")
      preceding_count = self.connection.execute(
        "SELECT COUNT(*) FROM revisions WHERE event_id=? AND revision<?", (event_id, revision)
      ).fetchone()[0]
      if preceding_count != revision - 1:
        raise ValueError("a revision requires all preceding revisions")
      if revision > 1:
        previous_timestamp = self.connection.execute(
          "SELECT detected_at FROM revisions WHERE event_id=? AND revision=?", (event_id, revision - 1)
        ).fetchone()[0]
        detected_at = self._timestamp_at_least(detected_at, previous_timestamp)
      if kind == "alarm" and self.connection.execute(
        "SELECT 1 FROM revisions WHERE event_id=? AND kind='alarm'", (event_id,)
      ).fetchone() is not None:
        raise ValueError("an event can only have one alarm revision")
      self.connection.execute(
        """
        INSERT INTO revisions(event_id, revision, kind, detected_at, message, state)
        VALUES (?, ?, ?, ?, ?, 'capturing')
        """,
        (event_id, revision, kind, detected_at, message),
      )

  def finish_capture(self, event_id: str, revision: int, media: dict[str, MediaData],
                     omissions: dict[str, str]) -> None:
    unknown = (set(media) | set(omissions)) - set(CAMERA_ROLES)
    if unknown or set(media) & set(omissions):
      raise ValueError("each known camera role must have exactly one capture outcome")
    media = dict(media)
    omissions = {role: self._bounded_reason(reason) for role, reason in omissions.items()}
    for role in CAMERA_ROLES:
      if role not in media and role not in omissions:
        omissions[role] = "capture_failed"

    row = self.connection.execute(
      """
      SELECT e.source, e.episode_started_at, e.schema_version, r.kind, r.detected_at, r.message, r.state
      FROM revisions r JOIN events e USING(event_id)
      WHERE r.event_id=? AND r.revision=?
      """,
      (event_id, revision),
    ).fetchone()
    if row is None or row["state"] != "capturing":
      raise ValueError("Sentry revision is not awaiting capture")

    written: dict[str, QueuedMedia] = {}
    for role, captured in list(media.items()):
      try:
        written[role] = self._write_media(event_id, revision, role, captured)
      except (AttributeError, OSError, TypeError, ValueError):
        omissions[role] = "capture_failed"
        media.pop(role, None)

    metadata = self._metadata(event_id, revision, dict(row), tuple(written.values()), omissions)
    try:
      with self.connection:
        for item in written.values():
          self.connection.execute(
            """
            INSERT INTO media(event_id, revision, role, path, sha256, size, width, height)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, revision, item.role, item.path, item.sha256, item.size, item.width, item.height),
          )
        for role, reason in omissions.items():
          self.connection.execute(
            "INSERT INTO media(event_id, revision, role, omission_reason) VALUES (?, ?, ?, ?)",
            (event_id, revision, role, self._bounded_reason(reason)),
          )
        self.connection.execute(
          "UPDATE revisions SET state='ready', capture_status=?, metadata_json=? WHERE event_id=? AND revision=?",
          (metadata["capture_status"], self._dump_metadata(metadata), event_id, revision),
        )
    except Exception:
      self._delete_paths(tuple(item.path for item in written.values()))
      self._remove_empty_revision_directory(event_id, revision)
      raise
    self.enforce_media_quota()
    self._secure_sidecars()

  def recover_interrupted_captures(self) -> int:
    rows = self.connection.execute(
      "SELECT event_id, revision FROM revisions WHERE state='capturing' ORDER BY created_at, event_id, revision"
    ).fetchall()
    recovered = 0
    for row in rows:
      directory = self._revision_directory(row["event_id"], row["revision"])
      self._remove_tree_files(directory)
      self.finish_capture(row["event_id"], row["revision"], {}, {
        "wide": "stale_capture",
        "cabin": "stale_capture",
      })
      recovered += 1
    return recovered

  def next_pending(self, now: float) -> QueuedRevision | None:
    row = self.connection.execute(
      """
      SELECT r.event_id, r.revision, r.metadata_json, r.attempts FROM revisions r
      WHERE r.state='ready' AND r.next_attempt_at<=?
        AND (SELECT COUNT(*) FROM revisions preceding
          WHERE preceding.event_id=r.event_id AND preceding.revision<r.revision
            AND preceding.state='acknowledged')=r.revision-1
      ORDER BY r.created_at, r.event_id, r.revision LIMIT 1
      """,
      (now,),
    ).fetchone()
    if row is None:
      return None
    media_rows = self.connection.execute(
      """
      SELECT role, path, sha256, size, width, height FROM media
      WHERE event_id=? AND revision=? AND path IS NOT NULL ORDER BY role
      """,
      (row["event_id"], row["revision"]),
    ).fetchall()
    return QueuedRevision(
      event_id=row["event_id"],
      revision=row["revision"],
      metadata=json.loads(row["metadata_json"]),
      media=tuple(QueuedMedia(**dict(media_row)) for media_row in media_rows),
      attempts=row["attempts"],
    )

  def upload_work_due(self, now: float) -> bool:
    return self.connection.execute(
      """
      SELECT 1 FROM revisions r
      WHERE r.state IN ('ready', 'uploading') AND r.next_attempt_at<=?
        AND (SELECT COUNT(*) FROM revisions preceding
          WHERE preceding.event_id=r.event_id AND preceding.revision<r.revision
            AND preceding.state='acknowledged')=r.revision-1
      LIMIT 1
      """, (now,),
    ).fetchone() is not None

  def claim_pending(self, now: float) -> QueuedRevision | None:
    """Atomically freeze and lease the next wire payload before media is opened."""
    self.connection.execute("BEGIN IMMEDIATE")
    try:
      self.connection.execute(
        """
        UPDATE revisions SET state='ready', last_error='expired upload claim recovered'
        WHERE state='uploading' AND next_attempt_at<=?
        """, (now,),
      )
      row = self.connection.execute(
        """
        SELECT r.event_id, r.revision, r.metadata_json, r.attempts FROM revisions r
        WHERE r.state='ready' AND r.next_attempt_at<=?
          AND (SELECT COUNT(*) FROM revisions preceding
            WHERE preceding.event_id=r.event_id AND preceding.revision<r.revision
              AND preceding.state='acknowledged')=r.revision-1
        ORDER BY r.created_at, r.event_id, r.revision LIMIT 1
        """, (now,),
      ).fetchone()
      if row is None:
        self.connection.commit()
        return None
      media_rows = self.connection.execute(
        """
        SELECT role, path, sha256, size, width, height FROM media
        WHERE event_id=? AND revision=? AND path IS NOT NULL ORDER BY role
        """, (row["event_id"], row["revision"]),
      ).fetchall()
      updated = self.connection.execute(
        """
        UPDATE revisions SET state='uploading', attempts=attempts+1, last_attempt_at=?, next_attempt_at=?
        WHERE event_id=? AND revision=? AND state='ready' AND attempts=?
        """,
        (self._utc_now(), now + UPLOAD_CLAIM_SECONDS, row["event_id"], row["revision"], row["attempts"]),
      )
      if updated.rowcount != 1:
        raise sqlite3.OperationalError("Sentry upload claim was lost")
      self.connection.commit()
    except Exception:
      self.connection.rollback()
      raise
    return QueuedRevision(
      event_id=row["event_id"],
      revision=row["revision"],
      metadata=json.loads(row["metadata_json"]),
      media=tuple(QueuedMedia(**dict(media_row)) for media_row in media_rows),
      attempts=row["attempts"],
    )

  def recover_expired_upload_claims(self, now: float | None = None) -> int:
    now = datetime.now(UTC).timestamp() if now is None else now
    with self.connection:
      result = self.connection.execute(
        """
        UPDATE revisions SET state='ready', last_error='interrupted upload claim recovered'
        WHERE state='uploading' AND next_attempt_at<=?
        """, (now,),
      )
    return result.rowcount

  def record_attempt(self, event_id: str, revision: int) -> None:
    with self.connection:
      updated = self.connection.execute(
        "UPDATE revisions SET attempts=attempts+1, last_attempt_at=? WHERE event_id=? AND revision=? AND state='ready'",
        (self._utc_now(), event_id, revision),
      )
      if updated.rowcount != 1:
        raise ValueError("Sentry revision is not ready for upload")

  def revision_state(self, event_id: str, revision: int) -> str | None:
    row = self.connection.execute(
      "SELECT state FROM revisions WHERE event_id=? AND revision=?", (event_id, revision)
    ).fetchone()
    return row["state"] if row is not None else None

  def schedule_retry(self, event_id: str, revision: int, *, next_attempt_at: float,
                     error: str, http_status: int | None = None) -> None:
    with self.connection:
      self.connection.execute(
        """
        UPDATE revisions SET state='ready', next_attempt_at=?, last_error=?, last_http_status=?
        WHERE event_id=? AND revision=? AND state IN ('ready', 'uploading')
        """,
        (next_attempt_at, self._bounded_error(error), http_status, event_id, revision),
      )

  def mark_terminal(self, event_id: str, revision: int, *, error: str, http_status: int) -> None:
    with self.connection:
      self.connection.execute(
        """
        UPDATE revisions SET state='terminal', last_error=?, last_http_status=?
        WHERE event_id=? AND revision=? AND state IN ('ready', 'uploading')
        """,
        (self._bounded_error(error), http_status, event_id, revision),
      )

  def retry_terminal(self) -> int:
    with self.connection:
      result = self.connection.execute(
        """
        UPDATE revisions SET state='ready', next_attempt_at=0, last_error=NULL, last_http_status=NULL
        WHERE state='terminal' AND retryable=1
        """
      )
    return result.rowcount

  def acknowledge(self, event_id: str, revision: int, expected_media: dict[str, str], *, deleted: bool = False) -> bool:
    row = self.connection.execute(
      "SELECT state FROM revisions WHERE event_id=? AND revision=?", (event_id, revision)
    ).fetchone()
    if row is None or row["state"] not in ("ready", "uploading", "terminal"):
      return False
    local_media = {
      item["role"]: item["sha256"] for item in self.connection.execute(
        "SELECT role, sha256 FROM media WHERE event_id=? AND revision=? AND sha256 IS NOT NULL",
        (event_id, revision),
      )
    }
    if not deleted and local_media != expected_media:
      return False
    with self.connection:
      self.connection.execute(
        "UPDATE revisions SET state='acknowledged', acknowledged_at=?, last_error=NULL WHERE event_id=? AND revision=?",
        (self._utc_now(), event_id, revision),
      )
    # Keep paths in SQLite until unlink succeeds. A restart can then finish an
    # acknowledgement interrupted between the durable state change and cleanup.
    self.cleanup_acknowledged_media(event_id, revision)
    return True

  def cleanup_acknowledged_media(self, event_id: str | None = None, revision: int | None = None) -> int:
    where = "r.state='acknowledged'"
    values: tuple[object, ...] = ()
    if event_id is not None:
      if revision is None:
        raise ValueError("revision is required with event_id")
      where += " AND m.event_id=? AND m.revision=?"
      values = (event_id, revision)
    rows = self.connection.execute(
      f"""
      SELECT m.event_id, m.revision, m.path FROM media m
      JOIN revisions r USING(event_id, revision) WHERE {where}
      """, values,
    ).fetchall()
    paths = tuple(row["path"] for row in rows if row["path"] is not None)
    if paths:
      self._delete_paths(paths)
    keys = {(row["event_id"], row["revision"]) for row in rows}
    if event_id is not None:
      keys.add((event_id, revision))
    with self.connection:
      for acknowledged_event_id, acknowledged_revision in keys:
        self.connection.execute(
          "DELETE FROM media WHERE event_id=? AND revision=?",
          (acknowledged_event_id, acknowledged_revision),
        )
        self._cleanup_closed_event_locked(acknowledged_event_id)
    for acknowledged_event_id, acknowledged_revision in keys:
      self._remove_empty_revision_directory(acknowledged_event_id, acknowledged_revision)
    return len(paths)

  def close_event(self, event_id: str) -> None:
    with self.connection:
      self.connection.execute("UPDATE events SET closed=1 WHERE event_id=?", (event_id,))
      self._cleanup_closed_event_locked(event_id)

  def close_open_events(self) -> int:
    with self.connection:
      result = self.connection.execute("UPDATE events SET closed=1 WHERE closed=0")
      for row in self.connection.execute("SELECT event_id FROM events WHERE closed=1").fetchall():
        self._cleanup_closed_event_locked(row["event_id"])
    return result.rowcount

  def reconcile_media_tree(self) -> int:
    """Validate the private media tree and remove safe, unreferenced files."""
    referenced: dict[Path, int] = {}
    for row in self.connection.execute(
        "SELECT event_id, revision, role, path, size FROM media WHERE path IS NOT NULL"):
      path = Path(row["path"]).absolute()
      try:
        relative = path.relative_to(self.media_root.absolute())
      except ValueError as exc:
        raise OSError(f"Sentry media manifest points outside the media directory: {path}") from exc
      expected = (row["event_id"], str(row["revision"]), f"{row['role']}.jpg")
      if relative.parts != expected or path in referenced:
        raise OSError(f"Sentry media manifest has an invalid path: {path}")
      referenced[path] = row["size"]

    on_disk: set[Path] = set()
    revision_directories: list[tuple[str, int]] = []
    for event_directory in self.media_root.iterdir():
      event_info = event_directory.lstat()
      if (stat.S_ISLNK(event_info.st_mode) or not stat.S_ISDIR(event_info.st_mode) or
          stat.S_IMODE(event_info.st_mode) != 0o700):
        raise OSError(f"unsafe Sentry media event directory: {event_directory}")
      try:
        parsed_event_id = UUID(event_directory.name)
      except ValueError as exc:
        raise OSError(f"invalid Sentry media event directory: {event_directory}") from exc
      if parsed_event_id.version != 4 or str(parsed_event_id) != event_directory.name:
        raise OSError(f"invalid Sentry media event directory: {event_directory}")
      for revision_directory in event_directory.iterdir():
        revision_info = revision_directory.lstat()
        if (stat.S_ISLNK(revision_info.st_mode) or not stat.S_ISDIR(revision_info.st_mode) or
            stat.S_IMODE(revision_info.st_mode) != 0o700 or not self._valid_revision_directory_name(revision_directory.name)):
          raise OSError(f"unsafe Sentry media revision directory: {revision_directory}")
        revision_directories.append((event_directory.name, int(revision_directory.name)))
        for media_path in revision_directory.iterdir():
          media_info = media_path.lstat()
          if (stat.S_ISLNK(media_info.st_mode) or not stat.S_ISREG(media_info.st_mode) or
              stat.S_IMODE(media_info.st_mode) != 0o600):
            raise OSError(f"unsafe Sentry media file: {media_path}")
          absolute_path = media_path.absolute()
          expected_size = referenced.get(absolute_path)
          if expected_size is not None and media_info.st_size != expected_size:
            raise OSError(f"Sentry media file size does not match its manifest: {media_path}")
          on_disk.add(absolute_path)

    missing = set(referenced) - on_disk
    if missing:
      raise OSError(f"Sentry media file is missing: {min(str(path) for path in missing)}")
    orphaned = tuple(str(path) for path in sorted(on_disk - set(referenced)))
    self._delete_paths(orphaned)
    for event_id, revision in revision_directories:
      self._remove_empty_revision_directory(event_id, revision)
    return len(orphaned)

  def stats(self) -> OutboxStats:
    row = self.connection.execute(
      """
      SELECT
        (SELECT COUNT(*) FROM revisions WHERE state IN (
          'capturing', 'ready', 'uploading', 'evicting_ready', 'evicting_terminal', 'evicting_uncertain'
        )) AS pending,
        (SELECT COUNT(*) FROM revisions WHERE state='terminal') AS terminal,
        COALESCE((SELECT SUM(size) FROM media WHERE path IS NOT NULL), 0) AS media_bytes
      """
    ).fetchone()
    return OutboxStats(row["pending"], row["terminal"], row["media_bytes"])

  def enforce_media_quota(self) -> int:
    # Acknowledgement is committed before media cleanup so it is crash-safe.
    # Always finish that cleanup before quota selection: a transient unlink or
    # fsync failure must not make already-delivered bytes evict another event.
    self.cleanup_acknowledged_media()
    self.recover_interrupted_evictions()
    evicted = 0
    while True:
      reserved = self._reserve_quota_candidate()
      if reserved is None:
        break
      self._finish_reserved_eviction(*reserved)
      evicted += 1
    return evicted

  def _reserve_quota_candidate(self) -> tuple[str, int, str] | None:
    """Durably exclude one eligible bundle from upload before filesystem work."""
    # Serialize eligibility and reservation with claim_pending. Filesystem
    # cleanup happens only after this state is durably committed, so a partial
    # unlink can never expose ready metadata that references missing bytes.
    self.connection.execute("BEGIN IMMEDIATE")
    try:
      media_bytes = self.connection.execute(
        "SELECT COALESCE(SUM(size), 0) FROM media WHERE path IS NOT NULL"
      ).fetchone()[0]
      if media_bytes <= self.media_quota_bytes:
        self.connection.commit()
        return None

      candidate = self.connection.execute(
        """
        SELECT r.event_id, r.revision, r.state, r.attempts FROM revisions r
        WHERE (r.state='ready' OR (r.state='terminal' AND r.retryable=1))
          AND EXISTS (
            SELECT 1 FROM media m
            WHERE m.event_id=r.event_id AND m.revision=r.revision AND m.path IS NOT NULL
          )
        ORDER BY r.created_at, r.event_id, r.revision LIMIT 1
        """
      ).fetchone()
      if candidate is None:
        self.connection.commit()
        return None
      source_state = candidate["state"]
      if source_state == "terminal":
        reserved_state = "evicting_terminal"
      elif candidate["attempts"] == 0:
        reserved_state = "evicting_ready"
      else:
        reserved_state = "evicting_uncertain"
      if reserved_state == "evicting_ready":
        update_sql = """
          UPDATE revisions SET state=?
          WHERE event_id=? AND revision=? AND state=? AND attempts=0
        """
      elif reserved_state == "evicting_terminal":
        update_sql = """
          UPDATE revisions SET state=?
          WHERE event_id=? AND revision=? AND state=? AND retryable=1
        """
      else:
        update_sql = """
          UPDATE revisions SET state=?
          WHERE event_id=? AND revision=? AND state=? AND attempts>0
        """
      updated = self.connection.execute(
        update_sql, (reserved_state, candidate["event_id"], candidate["revision"], source_state)
      )
      if updated.rowcount != 1:
        raise sqlite3.OperationalError("Sentry quota reservation was lost")
      self.connection.commit()
      return candidate["event_id"], candidate["revision"], reserved_state
    except Exception:
      self.connection.rollback()
      raise

  def recover_interrupted_evictions(self) -> int:
    rows = self.connection.execute(
      """
      SELECT event_id, revision, state FROM revisions
      WHERE state IN ('evicting_ready', 'evicting_terminal', 'evicting_uncertain')
      ORDER BY created_at, event_id, revision
      """
    ).fetchall()
    recovered = 0
    for row in rows:
      if self._finish_reserved_eviction(row["event_id"], row["revision"], row["state"]):
        recovered += 1
    return recovered

  def _finish_reserved_eviction(self, event_id: str, revision: int, reserved_state: str) -> bool:
    if reserved_state not in ("evicting_ready", "evicting_terminal", "evicting_uncertain"):
      raise ValueError("invalid Sentry quota reservation state")
    # A second recovery process must not race the idempotent filesystem pass.
    self.connection.execute("BEGIN IMMEDIATE")
    try:
      current = self.connection.execute(
        "SELECT state FROM revisions WHERE event_id=? AND revision=?", (event_id, revision)
      ).fetchone()
      if current is None or current["state"] != reserved_state:
        self.connection.commit()
        return False
      if reserved_state == "evicting_uncertain":
        self._terminalize_and_evict_bundle(event_id, revision, reserved_state)
      else:
        final_state = "ready" if reserved_state == "evicting_ready" else "terminal"
        self._evict_bundle(event_id, revision, reserved_state, final_state)
      self.connection.commit()
      return True
    except Exception:
      self.connection.rollback()
      raise

  def _evict_bundle(self, event_id: str, revision: int, reserved_state: str, final_state: str) -> None:
    if final_state not in ("ready", "terminal"):
      raise ValueError("invalid Sentry quota final state")
    paths = tuple(item["path"] for item in self.connection.execute(
      "SELECT path FROM media WHERE event_id=? AND revision=? AND path IS NOT NULL", (event_id, revision)
    ))
    row = self.connection.execute(
      """
      SELECT e.source, e.episode_started_at, e.schema_version, r.kind, r.detected_at, r.message
      FROM revisions r JOIN events e USING(event_id) WHERE r.event_id=? AND r.revision=?
      """, (event_id, revision)
    ).fetchone()
    existing_omissions = {
      item["role"]: item["omission_reason"] for item in self.connection.execute(
        "SELECT role, omission_reason FROM media WHERE event_id=? AND revision=? AND omission_reason IS NOT NULL",
        (event_id, revision),
      )
    }
    evicted_roles = {
      item["role"] for item in self.connection.execute(
        "SELECT role FROM media WHERE event_id=? AND revision=? AND path IS NOT NULL", (event_id, revision)
      )
    }
    omissions = {**existing_omissions, **dict.fromkeys(evicted_roles, "queue_quota")}
    metadata = self._metadata(event_id, revision, dict(row), (), omissions)
    self._delete_paths(paths)
    for role in evicted_roles:
      self.connection.execute(
        """
        UPDATE media SET path=NULL, sha256=NULL, size=NULL, width=NULL, height=NULL, omission_reason='queue_quota'
        WHERE event_id=? AND revision=? AND role=?
        """, (event_id, revision, role)
      )
    updated = self.connection.execute(
      """
      UPDATE revisions SET state=?, capture_status=?, metadata_json=?
      WHERE event_id=? AND revision=? AND state=?
      """,
      (final_state, metadata["capture_status"], self._dump_metadata(metadata), event_id, revision, reserved_state),
    )
    if updated.rowcount != 1:
      raise sqlite3.OperationalError("Sentry quota candidate changed before rewrite")
    self._remove_empty_revision_directory(event_id, revision)

  def _terminalize_and_evict_bundle(self, event_id: str, revision: int, reserved_state: str) -> None:
    paths = tuple(item["path"] for item in self.connection.execute(
      "SELECT path FROM media WHERE event_id=? AND revision=? AND path IS NOT NULL", (event_id, revision)
    ))
    self._delete_paths(paths)
    # The metadata may already have reached RTZ. Keep its media manifests and
    # wire JSON byte-for-byte immutable, record why bytes are unavailable only
    # in local state, and stop automatic/manual retries to avoid a 409 replay.
    self.connection.execute(
      """
      UPDATE media SET path=NULL, local_omission_reason='queue_quota'
      WHERE event_id=? AND revision=? AND path IS NOT NULL
      """, (event_id, revision)
    )
    updated = self.connection.execute(
      """
      UPDATE revisions SET state='terminal', retryable=0,
        last_error='local media evicted to enforce the 1 GiB Sentry quota'
      WHERE event_id=? AND revision=? AND state=? AND attempts>0
      """, (event_id, revision, reserved_state)
    )
    if updated.rowcount != 1:
      raise sqlite3.OperationalError("Sentry quota candidate changed before terminal eviction")
    self._remove_empty_revision_directory(event_id, revision)

  def _metadata(self, event_id: str, revision: int, row: dict[str, Any], media: tuple[QueuedMedia, ...],
                omissions: dict[str, str]) -> dict[str, Any]:
    media_roles = {item.role for item in media}
    if media_roles == set(CAMERA_ROLES):
      capture_status = "complete"
    elif media_roles:
      capture_status = "partial"
    elif omissions and all(reason == "queue_quota" for reason in omissions.values()):
      capture_status = "omitted"
    else:
      capture_status = "failed"
    return {
      "schema_version": row["schema_version"],
      "event_id": event_id,
      "revision": revision,
      "kind": row["kind"],
      "source": row["source"],
      "episode_started_at": row["episode_started_at"],
      "detected_at": row["detected_at"],
      "message": row["message"],
      "capture_status": capture_status,
      "media": [
        {
          "role": item.role,
          "content_type": "image/jpeg",
          "sha256": item.sha256,
          "size": item.size,
          "width": item.width,
          "height": item.height,
        }
        for item in sorted(media, key=lambda item: item.role)
      ],
      "omitted_media": [
        {"role": role, "reason": self._bounded_reason(reason)}
        for role, reason in sorted(omissions.items())
      ],
    }

  def _write_media(self, event_id: str, revision: int, role: str, captured: MediaData) -> QueuedMedia:
    if role not in CAMERA_ROLES or captured.width <= 0 or captured.height <= 0:
      raise ValueError("invalid captured media dimensions or role")
    if not 4 <= len(captured.data) <= MAX_MEDIA_BYTES or not captured.data.startswith(b"\xff\xd8") or not captured.data.endswith(b"\xff\xd9"):
      raise ValueError("invalid captured JPEG")
    directory = self._prepare_revision_directory(event_id, revision)
    target = directory / f"{role}.jpg"
    if target.exists() or target.is_symlink():
      raise OSError(f"Sentry media file already exists: {target}")
    fd = -1
    temporary = ""
    target_created = False
    target_committed = False
    try:
      fd, temporary = tempfile.mkstemp(prefix=f".{role}.", dir=directory)
      os.fchmod(fd, 0o600)
      view = memoryview(captured.data)
      while view:
        written = os.write(fd, view)
        if written <= 0:
          raise OSError("short media write")
        view = view[written:]
      os.fsync(fd)
      os.close(fd)
      fd = -1
      os.replace(temporary, target)
      temporary = ""
      target_created = True
      target_info = target.lstat()
      if not stat.S_ISREG(target_info.st_mode) or stat.S_IMODE(target_info.st_mode) != 0o600:
        raise OSError(f"unsafe Sentry media file after write: {target}")
      self._fsync_directory(directory)
      target_committed = True
    except Exception:
      if target_created and not target_committed:
        try:
          self._delete_paths((str(target),))
        except OSError:
          pass
      raise
    finally:
      if fd >= 0:
        os.close(fd)
      if temporary:
        try:
          os.unlink(temporary)
        except FileNotFoundError:
          pass
    return QueuedMedia(
      role=role,
      path=str(target),
      sha256=hashlib.sha256(captured.data).hexdigest(),
      size=len(captured.data),
      width=captured.width,
      height=captured.height,
    )

  def _create_schema(self) -> None:
    # Every upload uses a new connection. Already-migrated readers must not take
    # a schema write lock or rescan all queued media on each one-second poll.
    version = self.connection.execute("PRAGMA user_version").fetchone()[0]
    if version == 2:
      return
    if version > 2:
      raise ValueError("unsupported future Sentry outbox schema")
    # SQLite cannot widen an existing CHECK constraint in place. Disable FK
    # actions only on this connection, then serialize discovery and the complete
    # table rebuild with writers/other initializers. Transactional DDL leaves
    # either the old schema or the complete new schema after a crash; do not use
    # executescript here because it implicitly commits a pending transaction.
    self.connection.execute("PRAGMA foreign_keys=OFF")
    try:
      self.connection.execute("BEGIN IMMEDIATE")
      version = self.connection.execute("PRAGMA user_version").fetchone()[0]
      if version == 2:
        # A concurrent initializer completed migration while we waited.
        self.connection.commit()
        return
      if version > 2:
        raise ValueError("unsupported future Sentry outbox schema")
      self.connection.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
          event_id TEXT PRIMARY KEY,
          source TEXT NOT NULL CHECK(source IN ('motion', 'manual_test')),
          episode_started_at TEXT NOT NULL,
          closed INTEGER NOT NULL DEFAULT 0 CHECK(closed IN (0, 1)),
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version IN (1, 2))
        )
        """
      )
      if "schema_version" not in {row["name"] for row in self.connection.execute("PRAGMA table_info(events)")}:
        self.connection.execute(
          "ALTER TABLE events ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version IN (1, 2))"
        )
      existing = self.connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='revisions'").fetchone()
      if existing is None:
        self._create_revision_table("revisions")
      elif version < 2:
        self._create_revision_table("sentry_revisions_v2")
        # Copy stored wire JSON as opaque text, never regenerate queued payloads:
        # they may already have been accepted by RTZ before a lost acknowledgement.
        columns = (
          "event_id, revision, kind, detected_at, message, capture_status, metadata_json, state, retryable, " +
          "attempts, next_attempt_at, last_attempt_at, last_http_status, last_error, acknowledged_at, created_at"
        )
        self.connection.execute(f"INSERT INTO sentry_revisions_v2 ({columns}) SELECT {columns} FROM revisions")
        self.connection.execute("DROP TABLE revisions")
        self.connection.execute("ALTER TABLE sentry_revisions_v2 RENAME TO revisions")
      self.connection.execute(
        "CREATE INDEX IF NOT EXISTS sentry_revisions_pending ON revisions(state, next_attempt_at, created_at)"
      )
      self._create_media_table()
      if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise sqlite3.IntegrityError("Sentry outbox migration found invalid foreign keys")
      self.connection.execute("PRAGMA user_version=2")
      self.connection.commit()
    except Exception:
      self.connection.rollback()
      raise
    finally:
      self.connection.execute("PRAGMA foreign_keys=ON")

  def _create_revision_table(self, table: str) -> None:
    if table not in ("revisions", "sentry_revisions_v2"):
      raise ValueError("invalid Sentry revision table name")
    self.connection.execute(
      f"""
      CREATE TABLE {table} (
        event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
        revision INTEGER NOT NULL CHECK(revision>=1 AND revision<={MAX_REVISION}),
        kind TEXT NOT NULL CHECK(kind IN ('warning', 'follow_up', 'alarm')),
        detected_at TEXT NOT NULL,
        message TEXT NOT NULL,
        capture_status TEXT CHECK(capture_status IN ('complete', 'partial', 'failed', 'omitted')),
        metadata_json TEXT,
        state TEXT NOT NULL CHECK(state IN (
          'capturing', 'ready', 'uploading', 'terminal', 'acknowledged',
          'evicting_ready', 'evicting_terminal', 'evicting_uncertain'
        )),
        retryable INTEGER NOT NULL DEFAULT 1 CHECK(retryable IN (0, 1)),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        last_attempt_at TEXT,
        last_http_status INTEGER,
        last_error TEXT,
        acknowledged_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(event_id, revision),
        CHECK((revision=1 AND kind='warning') OR (revision>=2 AND kind IN ('follow_up', 'alarm')))
      )
      """
    )

  def _create_media_table(self) -> None:
    self.connection.execute(
      """
      CREATE TABLE IF NOT EXISTS media (
        event_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('wide', 'cabin')),
        path TEXT,
        sha256 TEXT,
        size INTEGER,
        width INTEGER,
        height INTEGER,
        omission_reason TEXT,
        local_omission_reason TEXT CHECK(local_omission_reason IN ('queue_quota')),
        PRIMARY KEY(event_id, revision, role),
        FOREIGN KEY(event_id, revision) REFERENCES revisions(event_id, revision) ON DELETE CASCADE,
        CHECK((path IS NOT NULL AND sha256 IS NOT NULL AND size IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL
               AND omission_reason IS NULL AND local_omission_reason IS NULL)
           OR (path IS NULL AND sha256 IS NULL AND size IS NULL AND width IS NULL AND height IS NULL
               AND omission_reason IS NOT NULL AND local_omission_reason IS NULL)
           OR (path IS NULL AND sha256 IS NOT NULL AND size IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL
               AND omission_reason IS NULL AND local_omission_reason='queue_quota'))
      )
      """
    )

  @staticmethod
  def _validate_revision(event_id: str, revision: int, kind: str, source: str, episode_started_at: str,
                         detected_at: str, message: str, schema_version: int = 1) -> None:
    try:
      parsed = UUID(event_id)
    except ValueError as exc:
      raise ValueError("event_id must be a UUID") from exc
    if parsed.version != 4 or str(parsed) != event_id:
      raise ValueError("event_id must be a canonical UUIDv4")
    if type(schema_version) is not int or schema_version not in (1, 2):
      raise ValueError("invalid Sentry event schema version")
    if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
      raise ValueError("invalid Sentry event revision number")
    valid_pair = ((revision, kind) in ((1, "warning"), (2, "alarm")) if schema_version == 1 else
                  (revision == 1 and kind == "warning") or (revision >= 2 and kind in ("follow_up", "alarm")))
    if not valid_pair:
      raise ValueError("revision and kind do not match")
    if source not in ("motion", "manual_test") or (source == "manual_test" and revision != 1):
      raise ValueError("invalid Sentry event source")
    for name, value in (("episode_started_at", episode_started_at), ("detected_at", detected_at)):
      if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"invalid {name}")
      try:
        parsed_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
      except ValueError as exc:
        raise ValueError(f"invalid {name}") from exc
      if parsed_time.tzinfo is None:
        raise ValueError(f"{name} must contain a timezone")
    if not isinstance(message, str) or not message or len(message.encode("utf-8")) > 512:
      raise ValueError("invalid Sentry event message")

  @staticmethod
  def _dump_metadata(metadata: dict[str, Any]) -> str:
    encoded = json.dumps(metadata, allow_nan=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode()) > 32 * 1024:
      raise ValueError("Sentry event metadata exceeds request limit")
    return encoded

  @staticmethod
  def _timestamp_at_least(value: str, minimum: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    parsed_minimum = datetime.fromisoformat(minimum.replace("Z", "+00:00"))
    return minimum if parsed < parsed_minimum else value

  @staticmethod
  def _bounded_reason(reason: str) -> str:
    if not isinstance(reason, str) or reason not in OMISSION_REASONS:
      raise ValueError(f"invalid Sentry media omission reason: {reason!r}")
    return reason

  @staticmethod
  def _bounded_error(error: str) -> str:
    return str(error).replace("\x00", "")[:512]

  @staticmethod
  def _utc_now() -> str:
    return datetime.now(UTC).isoformat()

  def _revision_directory(self, event_id: str, revision: int) -> Path:
    # event_id and revision are validated before reaching filesystem operations.
    return self.media_root / event_id / str(revision)

  @staticmethod
  def _valid_revision_directory_name(name: str) -> bool:
    return (1 <= len(name) <= 10 and name.isascii() and name.isdigit() and not name.startswith("0") and
            int(name) <= MAX_REVISION)

  def _prepare_revision_directory(self, event_id: str, revision: int) -> Path:
    event_directory = self._ensure_private_child(self.media_root, event_id)
    return self._ensure_private_child(event_directory, str(revision))

  def _ensure_private_directory(self, path: Path) -> None:
    created = False
    try:
      path.mkdir(mode=0o700, parents=True)
      created = True
    except FileExistsError:
      pass
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
      raise OSError(f"unsafe Sentry media directory: {path}")
    if created:
      os.chmod(path, 0o700)
      self._fsync_directory(path.parent)
    elif stat.S_IMODE(info.st_mode) != 0o700:
      raise OSError(f"unsafe Sentry media directory permissions: {path}")

  def _ensure_private_child(self, parent: Path, name: str) -> Path:
    parent_info = parent.lstat()
    if (stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode) or
        stat.S_IMODE(parent_info.st_mode) != 0o700):
      raise OSError(f"unsafe Sentry media parent: {parent}")
    path = parent / name
    created = False
    try:
      os.mkdir(path, 0o700)
      created = True
    except FileExistsError:
      pass
    child_info = path.lstat()
    if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISDIR(child_info.st_mode):
      raise OSError(f"unsafe Sentry media directory: {path}")
    if created:
      os.chmod(path, 0o700)
      self._fsync_directory(parent)
    elif stat.S_IMODE(child_info.st_mode) != 0o700:
      raise OSError(f"unsafe Sentry media directory permissions: {path}")
    return path

  @staticmethod
  def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
      os.fsync(fd)
    finally:
      os.close(fd)

  def _delete_paths(self, paths: tuple[str, ...]) -> None:
    synced_directories: set[Path] = set()
    media_root = self.media_root.absolute()
    for raw_path in paths:
      path = Path(raw_path).absolute()
      try:
        relative = path.relative_to(media_root)
      except ValueError as exc:
        raise OSError(f"refusing to delete media outside Sentry root: {path}") from exc
      if len(relative.parts) != 3:
        raise OSError(f"refusing to delete malformed Sentry media path: {path}")
      containing_directory_exists = True
      for directory in (media_root, path.parent.parent, path.parent):
        try:
          directory_info = directory.lstat()
        except FileNotFoundError:
          containing_directory_exists = False
          break
        if (stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode) or
            stat.S_IMODE(directory_info.st_mode) != 0o700):
          raise OSError(f"unsafe Sentry media directory during deletion: {directory}")
      # Sync the containing directory even when recovery finds this path already
      # absent. A previous attempt may have unlinked it and then failed before
      # that directory entry became durable.
      if containing_directory_exists:
        synced_directories.add(path.parent)
      try:
        path_info = path.lstat()
      except FileNotFoundError:
        continue
      if not (stat.S_ISREG(path_info.st_mode) or stat.S_ISLNK(path_info.st_mode)):
        raise OSError(f"unsafe Sentry media entry during deletion: {path}")
      path.unlink()
      synced_directories.add(path.parent)
    for directory in synced_directories:
      self._fsync_directory(directory)

  def _remove_tree_files(self, path: Path) -> None:
    if not path.is_dir() or path.is_symlink():
      return
    self._delete_paths(tuple(str(child) for child in path.iterdir()
                             if (child.is_file() and not child.is_symlink()) or child.is_symlink()))

  def _remove_empty_revision_directory(self, event_id: str, revision: int) -> None:
    directory = self._revision_directory(event_id, revision)
    try:
      directory_info = directory.lstat()
      event_info = directory.parent.lstat()
      if (stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode) or
          stat.S_IMODE(directory_info.st_mode) != 0o700 or stat.S_ISLNK(event_info.st_mode) or
          not stat.S_ISDIR(event_info.st_mode) or stat.S_IMODE(event_info.st_mode) != 0o700):
        return
      directory.rmdir()
      self._fsync_directory(directory.parent)
    except OSError:
      return
    try:
      directory.parent.rmdir()
      self._fsync_directory(self.media_root)
    except OSError:
      return

  def _secure_sidecars(self) -> None:
    # SQLite's Unix VFS creates sidecars with the private database's mode.
    # Validate metadata only. Opening and closing an extra descriptor for a
    # live -shm file drops this process's SQLite POSIX locks, allowing another
    # reader to truncate the WAL index underneath an active mapping (SIGBUS).
    # See https://sqlite.org/howtocorrupt.html#_posix_advisory_locks_canceled_by_a_separate_thread_doing_close_
    for suffix in ("-wal", "-shm", "-journal"):
      sidecar = Path(str(self.path) + suffix)
      try:
        path_info = sidecar.lstat()
      except FileNotFoundError:
        continue
      if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise OSError(f"unsafe Sentry outbox sidecar: {sidecar}")
      previous = self._sidecar_inodes.get(suffix)
      if previous is not None and previous != (path_info.st_dev, path_info.st_ino):
        raise OSError(f"Sentry outbox sidecar changed while open: {sidecar}")
      if stat.S_IMODE(path_info.st_mode) != 0o600:
        raise OSError(f"unsafe Sentry outbox sidecar permissions: {sidecar}")
      self._sidecar_inodes[suffix] = (path_info.st_dev, path_info.st_ino)

  def _cleanup_closed_event_locked(self, event_id: str) -> None:
    closed = self.connection.execute("SELECT closed FROM events WHERE event_id=?", (event_id,)).fetchone()
    if closed is None or not closed["closed"]:
      return
    outstanding = self.connection.execute(
      "SELECT 1 FROM revisions WHERE event_id=? AND state!='acknowledged' LIMIT 1", (event_id,)
    ).fetchone()
    remaining_media = self.connection.execute(
      "SELECT 1 FROM media WHERE event_id=? LIMIT 1", (event_id,)
    ).fetchone()
    if outstanding is not None or remaining_media is not None:
      return
    # Keep every acknowledged predecessor as the durable delivery-order barrier
    # while later revisions remain pending. Purge the group only when every revision is
    # acknowledged and its local media cleanup has completed.
    self.connection.execute("DELETE FROM revisions WHERE event_id=?", (event_id,))
    self.connection.execute("DELETE FROM events WHERE event_id=?", (event_id,))
