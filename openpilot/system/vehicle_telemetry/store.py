"""Boot-local checkpoints and a durable, acknowledged-only telemetry outbox."""
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path

from openpilot.system.vehicle_telemetry.state import DURABLE_KEY, MAX_ORDER, valid_durable, valid_snapshot

BATCH_LIMIT = 100
BYTE_LIMIT = 1024 * 1024


def encode(value):
  return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def legacy_state():
  # Read-only migration: never initialize Params or invent a new native key.
  prefix = os.environ.get("OPENPILOT_PREFIX", "d")
  for root in ("/data/vehicle_telemetry_params", "/persist/vehicle_telemetry_params"):
    try:
      stored = json.loads((Path(root) / prefix / DURABLE_KEY).read_text())
    except FileNotFoundError:
      continue
    if not valid_durable(stored):
      raise ValueError("Invalid legacy telemetry ordering; refusing to reset it")
    return stored
  return {"collector_epoch": 0, "snapshot": None}


def connect(path):
  path = Path(path)
  path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
  db = sqlite3.connect(path, timeout=10, isolation_level=None)
  os.chmod(path, 0o600)
  db.row_factory = sqlite3.Row
  db.execute("PRAGMA journal_mode=WAL")
  db.execute("PRAGMA synchronous=FULL")
  return db


class Outbox:
  def __init__(self, path, boot, legacy=None):
    self.db = connect(path)
    self.boot = boot
    self.db.executescript("""
      CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS checkpoints (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', claim TEXT, claim_boot TEXT, claim_until INTEGER,
        retry_boot TEXT, retry_at INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT);
    """)
    self.db.execute("BEGIN IMMEDIATE")
    try:
      if self.db.execute("SELECT 1 FROM metadata WHERE key='epoch'").fetchone() is None:
        prior = legacy_state() if legacy is None else legacy
        self.set_meta("epoch", prior["collector_epoch"])
        self.set_meta("seq", 0)
        if valid_snapshot(prior.get("snapshot")):
          self.set_meta("latest", prior["snapshot"])
      self.db.execute("COMMIT")
    except Exception:
      self.db.execute("ROLLBACK")
      raise

  def close(self):
    self.db.close()

  def set_meta(self, key, value):
    self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, encode(value)))

  def meta(self, key, default=None):
    row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default

  def new_epoch(self):
    self.db.execute("BEGIN IMMEDIATE")
    try:
      epoch = self.meta("epoch") + 1
      if epoch >= MAX_ORDER:
        raise ValueError("Telemetry ordering exhausted")
      self.set_meta("epoch", epoch)
      self.set_meta("seq", 0)
      self.db.execute("COMMIT")
      return epoch
    except Exception:
      self.db.execute("ROLLBACK")
      raise

  def next_order(self):
    self.db.execute("BEGIN IMMEDIATE")
    try:
      seq = self.meta("seq") + 1
      epoch = self.meta("epoch")
      if seq > MAX_ORDER:
        raise ValueError("Telemetry ordering exhausted")
      self.set_meta("seq", seq)
      self.db.execute("COMMIT")
      return epoch, seq
    except Exception:
      self.db.execute("ROLLBACK")
      raise

  def save_latest(self, snapshot):
    previous = self.meta("latest")
    if previous is None or (snapshot["collector_epoch"], snapshot["snapshot_seq"]) > (previous["collector_epoch"], previous["snapshot_seq"]):
      self.set_meta("latest", snapshot)

  def enqueue(self, record_id, payload):
    raw = encode(payload)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    self.db.execute("BEGIN IMMEDIATE")
    try:
      row = self.db.execute("SELECT digest FROM records WHERE id=?", (record_id,)).fetchone()
      if row is not None and row[0] != digest:
        raise ValueError("Immutable telemetry record changed")
      self.db.execute("INSERT OR IGNORE INTO records(id,digest,payload) VALUES (?,?,?)", (record_id, digest, raw))
      self.db.execute("DELETE FROM checkpoints WHERE id=?", (record_id,))
      self.db.execute("COMMIT")
    except Exception:
      self.db.execute("ROLLBACK")
      raise

  def checkpoint(self, record_id, payload):
    self.db.execute("BEGIN IMMEDIATE")
    try:
      self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?)", (record_id, encode(payload)))
      self.save_latest(payload["snapshot"])
      self.db.execute("COMMIT")
    except Exception:
      self.db.execute("ROLLBACK")
      raise

  def recover_checkpoints(self, segment, staged_ids):
    for row in self.db.execute("SELECT * FROM checkpoints").fetchall():
      payload = json.loads(row["payload"])
      if row["id"] in staged_ids:
        continue
      if payload["boot_id"] != self.boot or segment != (payload["route"], payload["segment"]):
        # This checkpoint already has trusted UTC. Never rebase it using this
        # boot's time; a reboot only establishes that its segment has finished.
        existing = self.db.execute("SELECT 1 FROM records WHERE id=?", (row["id"],)).fetchone()
        if existing is None:
          self.enqueue(row["id"], payload)
        else:
          self.db.execute("DELETE FROM checkpoints WHERE id=?", (row["id"],))

  @staticmethod
  def body(rows):
    # Embed the stored JSON verbatim: the digest covers these exact payload bytes.
    items = ['{"record_id":' + encode(r["id"]) + ',"payload_sha256":' + encode(r["digest"]) + ',"payload":' + r["payload"] + '}' for r in rows]
    return ('{"version":1,"records":[' + ','.join(items) + ']}').encode()

  def claim(self, now):
    self.db.execute("BEGIN IMMEDIATE")
    try:
      self.db.execute("UPDATE records SET state='pending',claim=NULL WHERE state='sending' AND (claim_boot != ? OR claim_until <= ?)",
                      (self.boot, now))
      available = self.db.execute("SELECT * FROM records WHERE state='pending' AND " +
                                  "(retry_boot IS NULL OR retry_boot != ? OR retry_at <= ?) ORDER BY rowid LIMIT ?",
                                  (self.boot, now, BATCH_LIMIT)).fetchall()
      rows = []
      for row in available:
        if len(self.body(rows + [row])) > BYTE_LIMIT:
          if not rows:
            self.db.execute("UPDATE records SET state='quarantined',error='record_too_large' WHERE id=?", (row["id"],))
          break
        rows.append(row)
      token = str(uuid.uuid4())
      for row in rows:
        self.db.execute("UPDATE records SET state='sending',claim=?,claim_boot=?,claim_until=? WHERE id=?",
                        (token, self.boot, now + 120_000_000_000, row["id"]))
      self.db.execute("COMMIT")
      return token, rows
    except Exception:
      self.db.execute("ROLLBACK")
      raise

  def finish(self, token, rows, results, now, delay):
    acknowledgements = {r.get("record_id"): r for r in results if isinstance(r, dict)} if isinstance(results, list) else {}
    self.db.execute("BEGIN IMMEDIATE")
    try:
      for row in rows:
        ack = acknowledgements.get(row["id"], {})
        status = ack.get("status") if ack.get("payload_sha256") == row["digest"] else None
        if status in ("accepted", "duplicate"):
          # Retain a tiny durable receipt: a crash before staging cleanup must
          # never reconstruct the same immutable ID using a newer clock anchor.
          self.db.execute("UPDATE records SET state='acknowledged',payload='',claim=NULL WHERE id=? AND claim=?", (row["id"], token))
        elif status == "rejected":
          self.db.execute("UPDATE records SET state='quarantined',error=?,claim=NULL WHERE id=? AND claim=?",
                          (str(ack.get("error", "rejected"))[:128], row["id"], token))
        else:
          self.db.execute("UPDATE records SET state='pending',claim=NULL,retry_boot=?,retry_at=?,attempts=attempts+1 WHERE id=? AND claim=?",
                          (self.boot, now + int(delay * 1e9), row["id"], token))
      self.db.execute("COMMIT")
    except Exception:
      self.db.execute("ROLLBACK")
      raise


class Staging:
  def __init__(self, path, boot):
    self.db = connect(path)
    self.db.execute("CREATE TABLE IF NOT EXISTS stages (id TEXT PRIMARY KEY,boot TEXT NOT NULL,ready INTEGER NOT NULL,data TEXT NOT NULL)")
    self.db.execute("DELETE FROM stages WHERE boot != ?", (boot,))
    self.boot = boot

  def save(self, record, ready=False):
    self.db.execute("INSERT OR REPLACE INTO stages VALUES (?,?,?,?)", (record["record_id"], self.boot, int(ready), encode(record)))

  def records(self):
    return [(json.loads(row["data"]), bool(row["ready"])) for row in self.db.execute("SELECT * FROM stages ORDER BY rowid")]

  def remove(self, record_id):
    self.db.execute("DELETE FROM stages WHERE id=?", (record_id,))
