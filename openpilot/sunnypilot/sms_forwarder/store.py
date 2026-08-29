from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openpilot.sunnypilot.sms_forwarder.modem import StoredPDU
from openpilot.sunnypilot.sms_forwarder.pdu import DecodedPDU


MULTIPART_WINDOW = timedelta(hours=6)


@dataclass(frozen=True)
class QueuedMessage:
  message_id: str
  sim_iccid: str
  sender: str
  sent_at: str
  received_at: str
  body: str
  parts: int

  def api_dict(self) -> dict[str, object]:
    return {
      "message_id": self.message_id,
      "sim_iccid": self.sim_iccid,
      "sender": self.sender,
      "sent_at": self.sent_at,
      "received_at": self.received_at,
      "body": self.body,
      "parts": self.parts,
    }


@dataclass(frozen=True)
class CleanupLocation:
  sim_iccid: str
  storage: str
  index: int
  digest: str


@dataclass(frozen=True)
class CleanupMessage:
  message_id: str
  sim_iccid: str
  locations: tuple[CleanupLocation, ...]


class MessageStore:
  def __init__(self, path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    self.connection = sqlite3.connect(path, timeout=10)
    self.connection.row_factory = sqlite3.Row
    self.connection.execute("PRAGMA foreign_keys=ON")
    self.connection.execute("PRAGMA journal_mode=WAL")
    self.connection.execute("PRAGMA synchronous=FULL")
    self._create_schema()

  def close(self) -> None:
    self.connection.close()

  def add_pdu(self, stored: StoredPDU, decoded: DecodedPDU, sim_iccid: str, received_at: datetime) -> bool:
    concat = decoded.concat
    with self.connection:
      inserted = self.connection.execute(
        """
        INSERT OR IGNORE INTO parts (
          digest, sim_iccid, sender, sent_at, received_at, body,
          concat_reference, concat_reference_bits, part_number, part_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
          stored.digest,
          sim_iccid,
          decoded.sender,
          _utc_iso(decoded.sent_at),
          _utc_iso(received_at),
          decoded.body,
          concat.reference if concat else None,
          concat.reference_bits if concat else None,
          concat.sequence if concat else 1,
          concat.total if concat else 1,
        ),
      )
      self.connection.execute(
        """
        INSERT INTO locations (sim_iccid, storage, slot, digest) VALUES (?, ?, ?, ?)
        ON CONFLICT(sim_iccid, storage, slot) DO UPDATE SET digest=excluded.digest
        """,
        (sim_iccid, stored.storage, stored.index, stored.digest),
      )
      self._assemble_messages()
    return inserted.rowcount == 1

  def pending(self, limit: int = 50) -> list[QueuedMessage]:
    rows = self.connection.execute(
      """
      SELECT message_id, sim_iccid, sender, sent_at, received_at, body, parts
      FROM outbox WHERE acknowledged=0 ORDER BY created_at, message_id LIMIT ?
      """,
      (limit,),
    ).fetchall()
    return [QueuedMessage(**dict(row)) for row in rows]

  def mark_acknowledged(self, message_ids: list[str]) -> None:
    if not message_ids:
      return
    placeholders = ",".join("?" for _ in message_ids)
    with self.connection:
      self.connection.execute(f"UPDATE outbox SET acknowledged=1 WHERE message_id IN ({placeholders})", message_ids)

  def cleanup_messages(self) -> list[CleanupMessage]:
    messages = self.connection.execute(
      "SELECT message_id, sim_iccid FROM outbox WHERE acknowledged=1 ORDER BY created_at, message_id"
    ).fetchall()
    result = []
    for message in messages:
      rows = self.connection.execute(
        """
        SELECT l.sim_iccid, l.storage, l.slot AS 'index', l.digest
        FROM locations l JOIN parts p ON p.sim_iccid=l.sim_iccid AND p.digest=l.digest
        WHERE p.message_id=? ORDER BY l.storage, l.slot
        """,
        (message["message_id"],),
      ).fetchall()
      result.append(CleanupMessage(
        message_id=message["message_id"],
        sim_iccid=message["sim_iccid"],
        locations=tuple(CleanupLocation(**dict(row)) for row in rows),
      ))
    return result

  def discard_location(self, location: CleanupLocation) -> None:
    with self.connection:
      self.connection.execute(
        "DELETE FROM locations WHERE sim_iccid=? AND storage=? AND slot=? AND digest=?",
        (location.sim_iccid, location.storage, location.index, location.digest),
      )

  def finish_cleanup(self, message_id: str) -> bool:
    remaining = self.connection.execute(
      """
      SELECT COUNT(*) FROM locations l JOIN parts p ON p.sim_iccid=l.sim_iccid AND p.digest=l.digest
      WHERE p.message_id=?
      """,
      (message_id,),
    ).fetchone()[0]
    if remaining:
      return False
    with self.connection:
      self.connection.execute("DELETE FROM outbox WHERE message_id=?", (message_id,))
    return True

  def counts(self) -> tuple[int, int, int]:
    return tuple(self.connection.execute(
      "SELECT (SELECT COUNT(*) FROM parts), (SELECT COUNT(*) FROM outbox WHERE acknowledged=0), (SELECT COUNT(*) FROM outbox WHERE acknowledged=1)"
    ).fetchone())

  def _create_schema(self) -> None:
    self.connection.executescript(
      """
      CREATE TABLE IF NOT EXISTS outbox (
        message_id TEXT PRIMARY KEY,
        sim_iccid TEXT NOT NULL,
        sender TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        received_at TEXT NOT NULL,
        body TEXT NOT NULL,
        parts INTEGER NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      CREATE TABLE IF NOT EXISTS parts (
        digest TEXT NOT NULL,
        sim_iccid TEXT NOT NULL,
        sender TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        received_at TEXT NOT NULL,
        body TEXT NOT NULL,
        concat_reference INTEGER,
        concat_reference_bits INTEGER,
        part_number INTEGER NOT NULL,
        part_count INTEGER NOT NULL,
        message_id TEXT REFERENCES outbox(message_id) ON DELETE CASCADE,
        PRIMARY KEY (sim_iccid, digest)
      );
      CREATE INDEX IF NOT EXISTS idx_parts_assembly ON parts (
        sim_iccid, sender, concat_reference, concat_reference_bits, part_count, sent_at
      ) WHERE message_id IS NULL;
      CREATE TABLE IF NOT EXISTS locations (
        sim_iccid TEXT NOT NULL,
        storage TEXT NOT NULL CHECK(storage IN ('SM', 'ME')),
        slot INTEGER NOT NULL,
        digest TEXT NOT NULL,
        PRIMARY KEY (sim_iccid, storage, slot),
        FOREIGN KEY (sim_iccid, digest) REFERENCES parts(sim_iccid, digest) ON DELETE CASCADE
      );
      """
    )
    self.connection.commit()

  def _assemble_messages(self) -> None:
    singles = self.connection.execute(
      "SELECT * FROM parts WHERE message_id IS NULL AND concat_reference IS NULL ORDER BY received_at, digest"
    ).fetchall()
    for part in singles:
      self._create_outbox([part])

    multipart = self.connection.execute(
      """
      SELECT * FROM parts WHERE message_id IS NULL AND concat_reference IS NOT NULL
      ORDER BY sim_iccid, sender, concat_reference, concat_reference_bits, part_count, sent_at, digest
      """
    ).fetchall()
    grouped: dict[tuple[object, ...], list[sqlite3.Row]] = {}
    for part in multipart:
      key = (
        part["sim_iccid"], part["sender"], part["concat_reference"],
        part["concat_reference_bits"], part["part_count"],
      )
      grouped.setdefault(key, []).append(part)
    for parts in grouped.values():
      self._assemble_multipart_group(parts)

  def _assemble_multipart_group(self, parts: list[sqlite3.Row]) -> None:
    candidates: list[dict[int, sqlite3.Row]] = []
    anchors: list[datetime] = []
    for part in parts:
      sent_at = datetime.fromisoformat(part["sent_at"])
      selected = None
      for index, candidate in enumerate(candidates):
        if part["part_number"] not in candidate and abs(sent_at - anchors[index]) <= MULTIPART_WINDOW:
          selected = index
          break
      if selected is None:
        candidates.append({part["part_number"]: part})
        anchors.append(sent_at)
      else:
        candidates[selected][part["part_number"]] = part

    for candidate in candidates:
      total = next(iter(candidate.values()))["part_count"]
      if len(candidate) == total and set(candidate) == set(range(1, total + 1)):
        self._create_outbox([candidate[index] for index in range(1, total + 1)])

  def _create_outbox(self, parts: list[sqlite3.Row]) -> None:
    first = parts[0]
    digests = [part["digest"] for part in parts]
    identity = first["sim_iccid"] + "\0" + "\0".join(digests)
    message_id = hashlib.sha256(identity.encode()).hexdigest()
    sent_at = min(datetime.fromisoformat(part["sent_at"]) for part in parts).isoformat()
    received_at = max(datetime.fromisoformat(part["received_at"]) for part in parts).isoformat()
    self.connection.execute(
      """
      INSERT OR IGNORE INTO outbox (message_id, sim_iccid, sender, sent_at, received_at, body, parts)
      VALUES (?, ?, ?, ?, ?, ?, ?)
      """,
      (message_id, first["sim_iccid"], first["sender"], sent_at, received_at, "".join(part["body"] for part in parts), len(parts)),
    )
    placeholders = ",".join("?" for _ in digests)
    self.connection.execute(
      f"UPDATE parts SET message_id=? WHERE sim_iccid=? AND digest IN ({placeholders}) AND message_id IS NULL",
      (message_id, first["sim_iccid"], *digests),
    )


def default_store_path() -> str:
  root = os.getenv("SMS_FORWARDER_ROOT", "/data/sms_forwarder")
  return str(Path(root) / "queue.sqlite3")


def _utc_iso(value: datetime) -> str:
  if value.tzinfo is None:
    value = value.replace(tzinfo=UTC)
  return value.astimezone(UTC).isoformat()
