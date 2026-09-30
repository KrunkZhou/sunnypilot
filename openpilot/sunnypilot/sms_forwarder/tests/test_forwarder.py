import hashlib
import threading
from datetime import UTC, datetime

import pytest

from openpilot.sunnypilot.sms_forwarder.__main__ import INITIAL_RETRY, MAX_RETRY, RetryBackoff, SMSForwarder
from openpilot.sunnypilot.sms_forwarder.modem import PDURead, StoredPDU
from openpilot.sunnypilot.sms_forwarder.pdu import DecodedPDU, decode_sms_deliver
from openpilot.sunnypilot.sms_forwarder.store import MessageStore
from openpilot.sunnypilot.sms_forwarder.tests.helpers import concat_8, sms_deliver_pdu


ICCID = "8912345678901234567"


class FakeUploader:
  pass


class FakeModem:
  def __init__(self, iccid: str, reads: dict[tuple[str, int], PDURead] | None = None, delete_success: bool | None = True):
    self.iccid = iccid
    self.reads = reads or {}
    self.read_calls = []
    self.deleted = []
    self.delete_success = delete_success
    self.delete_results = {}

  def current_iccid(self) -> str:
    return self.iccid

  def scan(self) -> list[StoredPDU]:
    return []

  def read(self, storage: str, index: int) -> PDURead:
    self.read_calls.append((storage, index))
    return self.reads[(storage, index)]

  def delete(self, storage: str, index: int) -> bool | None:
    self.deleted.append((storage, index))
    return self.delete_results.get((storage, index), self.delete_success)


def queue_acknowledged(store: MessageStore, raw_pdu: str, index: int = 8) -> str:
  digest = hashlib.sha256(bytes.fromhex(raw_pdu)).hexdigest()
  stored = StoredPDU("SM", index, raw_pdu, digest)
  decoded = DecodedPDU("+14165551234", datetime.now(UTC), "hello", None)
  store.add_pdu(stored, decoded, ICCID, datetime.now(UTC))
  message_id = store.pending()[0].message_id
  store.mark_acknowledged([message_id])
  return message_id


def test_acknowledged_slot_is_hash_checked_then_deleted(tmp_path) -> None:
  raw_pdu = sms_deliver_pdu("hello")
  store = MessageStore(str(tmp_path / "queue.sqlite3"))
  queue_acknowledged(store, raw_pdu)
  modem = FakeModem(ICCID, {("SM", 8): PDURead("found", raw_pdu)})
  SMSForwarder("dongle", store, modem, FakeUploader()).cleanup()
  assert modem.deleted == [("SM", 8)]
  assert store.counts() == (0, 0, 0)
  store.close()


def test_reused_slot_is_never_deleted(tmp_path) -> None:
  store = MessageStore(str(tmp_path / "queue.sqlite3"))
  queue_acknowledged(store, sms_deliver_pdu("old"))
  modem = FakeModem(ICCID, {("SM", 8): PDURead("found", sms_deliver_pdu("new"))})
  SMSForwarder("dongle", store, modem, FakeUploader()).cleanup()
  assert modem.deleted == []
  assert store.counts() == (0, 0, 0)
  store.close()


def test_sim_swap_preserves_acknowledged_queue_and_slot(tmp_path) -> None:
  store = MessageStore(str(tmp_path / "queue.sqlite3"))
  queue_acknowledged(store, sms_deliver_pdu("hello"))
  modem = FakeModem("8999999999999999999")
  SMSForwarder("dongle", store, modem, FakeUploader()).cleanup()
  assert modem.deleted == []
  assert store.counts() == (1, 0, 1)
  store.close()


@pytest.mark.parametrize("failure", ["read", "delete"])
def test_cleanup_skips_storage_after_transient_failure_and_preserves_backlog(tmp_path, failure) -> None:
  store = MessageStore(str(tmp_path / "queue.sqlite3"))
  pdus = {("SM", index): sms_deliver_pdu(f"message {index}") for index in (8, 9)}
  for (_, index), raw_pdu in pdus.items():
    queue_acknowledged(store, raw_pdu, index)
  first = store.cleanup_messages()[0].locations[0]
  modem = FakeModem(ICCID, {
    slot: PDURead("retry") if failure == "read" else PDURead("found", raw_pdu)
    for slot, raw_pdu in pdus.items()
  }, delete_success=None)
  forwarder = SMSForwarder("dongle", store, modem, FakeUploader())

  forwarder.cleanup()

  assert modem.read_calls == [(first.storage, first.index)]
  assert modem.deleted == ([] if failure == "read" else [(first.storage, first.index)])
  assert store.counts() == (2, 0, 2)
  assert sum(len(message.locations) for message in store.cleanup_messages()) == 2

  modem.reads = {slot: PDURead("found", raw_pdu) for slot, raw_pdu in pdus.items()}
  modem.delete_success = True
  forwarder.cleanup()
  assert store.counts() == (0, 0, 0)
  store.close()


@pytest.mark.parametrize("failed_storage", ["SM", "ME"])
@pytest.mark.parametrize("failure", ["read", "delete"])
def test_cleanup_continues_other_storage_after_transient_failure(tmp_path, failed_storage, failure) -> None:
  store = MessageStore(str(tmp_path / "queue.sqlite3"))
  pdus = {
    ("SM", 8): sms_deliver_pdu("first ", udh=concat_8(42, 2, 1)),
    ("ME", 8): sms_deliver_pdu("second", udh=concat_8(42, 2, 2)),
    ("SM", 9): sms_deliver_pdu("SIM message"),
    ("ME", 9): sms_deliver_pdu("modem message"),
  }
  for (storage, index), raw_pdu in pdus.items():
    digest = hashlib.sha256(bytes.fromhex(raw_pdu)).hexdigest()
    store.add_pdu(StoredPDU(storage, index, raw_pdu, digest), decode_sms_deliver(raw_pdu), ICCID, datetime.now(UTC))
  store.mark_acknowledged([message.message_id for message in store.pending()])
  modem = FakeModem(ICCID, {
    slot: PDURead("retry") if failure == "read" and slot[0] == failed_storage else PDURead("found", raw_pdu)
    for slot, raw_pdu in pdus.items()
  })
  if failure == "delete":
    modem.delete_results = {slot: None for slot in pdus if slot[0] == failed_storage}
  forwarder = SMSForwarder("dongle", store, modem, FakeUploader())

  forwarder.cleanup()

  assert len([slot for slot in modem.read_calls if slot[0] == failed_storage]) == 1
  assert {slot for slot in modem.deleted if slot[0] != failed_storage} == {slot for slot in pdus if slot[0] != failed_storage}
  remaining = [location for message in store.cleanup_messages() for location in message.locations]
  assert {(location.storage, location.index) for location in remaining} == {slot for slot in pdus if slot[0] == failed_storage}
  assert store.counts() == (3, 0, 2)

  modem.reads = {slot: PDURead("found", raw_pdu) for slot, raw_pdu in pdus.items()}
  modem.delete_results.clear()
  forwarder.cleanup()
  assert store.counts() == (0, 0, 0)
  store.close()


def test_cleanup_explicit_delete_error_does_not_block_other_slots(tmp_path) -> None:
  store = MessageStore(str(tmp_path / "queue.sqlite3"))
  pdus = {("SM", index): sms_deliver_pdu(f"message {index}") for index in (8, 9)}
  for (_, index), raw_pdu in pdus.items():
    queue_acknowledged(store, raw_pdu, index)
  first = store.cleanup_messages()[0].locations[0]
  modem = FakeModem(ICCID, {slot: PDURead("found", raw_pdu) for slot, raw_pdu in pdus.items()})
  modem.delete_results[(first.storage, first.index)] = False

  SMSForwarder("dongle", store, modem, FakeUploader()).cleanup()

  assert set(modem.read_calls) == set(pdus)
  assert set(modem.deleted) == set(pdus)
  assert store.counts() == (1, 0, 1)
  assert store.cleanup_messages()[0].locations == (first,)
  store.close()


def test_run_uploads_before_acknowledged_cleanup(tmp_path, monkeypatch) -> None:
  events = []
  stop_event = threading.Event()
  uploader = FakeUploader()
  store = MessageStore(str(tmp_path / "queue.sqlite3"))
  forwarder = SMSForwarder("dongle", store, FakeModem(ICCID), uploader)

  def upload_once():
    events.append("upload")
    return None

  def cleanup():
    events.append("cleanup")
    stop_event.set()

  monkeypatch.setattr(forwarder, "scan", lambda: events.append("scan"))
  monkeypatch.setattr(forwarder, "cleanup", cleanup)
  monkeypatch.setattr(uploader, "upload_once", upload_once, raising=False)
  monkeypatch.setattr("openpilot.sunnypilot.sms_forwarder.__main__.time.monotonic", lambda: 0.0)

  forwarder.run(stop_event)

  assert events == ["scan", "upload", "cleanup"]
  store.close()


def test_retry_backoff_is_exponential_jittered_and_capped(monkeypatch) -> None:
  monkeypatch.setattr("openpilot.sunnypilot.sms_forwarder.__main__.random.uniform", lambda low, high: high)
  backoff = RetryBackoff()
  delays = [backoff.failure_delay() for _ in range(10)]
  assert delays[0] == INITIAL_RETRY * 1.25
  assert delays[1] == INITIAL_RETRY * 2 * 1.25
  assert delays[-1] == MAX_RETRY
  assert max(delays) == MAX_RETRY
  backoff.reset()
  assert backoff.current == INITIAL_RETRY
