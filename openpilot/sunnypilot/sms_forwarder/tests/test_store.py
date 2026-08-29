import hashlib
from datetime import UTC, datetime

from openpilot.sunnypilot.sms_forwarder.modem import StoredPDU
from openpilot.sunnypilot.sms_forwarder.pdu import ConcatInfo, DecodedPDU
from openpilot.sunnypilot.sms_forwarder.store import MessageStore


ICCID = "8912345678901234567"
NOW = datetime(2026, 8, 29, 1, 15, 34, tzinfo=UTC)
SENT = datetime(2026, 8, 29, 1, 15, 30, tzinfo=UTC)


def stored(label: str, storage: str = "SM", index: int = 1) -> StoredPDU:
  raw = label.encode().hex()
  return StoredPDU(storage, index, raw, hashlib.sha256(bytes.fromhex(raw)).hexdigest())


def decoded(body: str, concat: ConcatInfo | None = None) -> DecodedPDU:
  return DecodedPDU(sender="+14165551234", sent_at=SENT, body=body, concat=concat)


def test_deduplication_and_restart_recovery(tmp_path) -> None:
  path = tmp_path / "queue.sqlite3"
  store = MessageStore(str(path))
  first = stored("same PDU", "SM", 1)
  assert store.add_pdu(first, decoded("hello"), ICCID, NOW)
  assert not store.add_pdu(stored("same PDU", "ME", 2), decoded("hello"), ICCID, NOW)
  pending = store.pending()
  assert len(pending) == 1 and pending[0].body == "hello"
  assert store.counts() == (1, 1, 0)
  store.close()

  reopened = MessageStore(str(path))
  assert [message.message_id for message in reopened.pending()] == [pending[0].message_id]
  assert len(reopened.cleanup_messages()) == 0
  reopened.close()


def test_identical_pdu_on_different_sims_is_not_deduplicated(tmp_path) -> None:
  store = MessageStore(str(tmp_path / "queue.sqlite3"))
  pdu = stored("same PDU")
  assert store.add_pdu(pdu, decoded("first SIM"), ICCID, NOW)
  assert store.add_pdu(pdu, decoded("second SIM"), "8999999999999999999", NOW)
  assert {message.body for message in store.pending()} == {"first SIM", "second SIM"}
  store.close()


def test_incomplete_and_complete_multipart_reassembly(tmp_path) -> None:
  store = MessageStore(str(tmp_path / "queue.sqlite3"))
  part_one = ConcatInfo(reference=82, reference_bits=8, total=2, sequence=1)
  part_two = ConcatInfo(reference=82, reference_bits=8, total=2, sequence=2)
  store.add_pdu(stored("part one", index=4), decoded("Hello ", part_one), ICCID, NOW)
  assert store.counts() == (1, 0, 0)
  store.add_pdu(stored("part two", index=5), decoded("world", part_two), ICCID, NOW)
  messages = store.pending()
  assert len(messages) == 1
  assert messages[0].body == "Hello world"
  assert messages[0].parts == 2
  store.close()


def test_acknowledgement_waits_for_all_modem_locations(tmp_path) -> None:
  store = MessageStore(str(tmp_path / "queue.sqlite3"))
  pdu = stored("same PDU", "SM", 1)
  store.add_pdu(pdu, decoded("hello"), ICCID, NOW)
  store.add_pdu(stored("same PDU", "ME", 2), decoded("hello"), ICCID, NOW)
  message_id = store.pending()[0].message_id
  store.mark_acknowledged([message_id])
  cleanup = store.cleanup_messages()[0]
  assert {(location.storage, location.index) for location in cleanup.locations} == {("SM", 1), ("ME", 2)}
  store.discard_location(cleanup.locations[0])
  assert not store.finish_cleanup(message_id)
  store.discard_location(cleanup.locations[1])
  assert store.finish_cleanup(message_id)
  assert store.counts() == (0, 0, 0)
  store.close()
