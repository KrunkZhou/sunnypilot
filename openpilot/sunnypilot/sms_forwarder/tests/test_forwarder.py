import hashlib
from datetime import UTC, datetime

from openpilot.sunnypilot.sms_forwarder.__main__ import INITIAL_RETRY, MAX_RETRY, RetryBackoff, SMSForwarder
from openpilot.sunnypilot.sms_forwarder.modem import PDURead, StoredPDU
from openpilot.sunnypilot.sms_forwarder.pdu import DecodedPDU
from openpilot.sunnypilot.sms_forwarder.store import MessageStore
from openpilot.sunnypilot.sms_forwarder.tests.helpers import sms_deliver_pdu


ICCID = "8912345678901234567"


class FakeUploader:
  pass


class FakeModem:
  def __init__(self, iccid: str, reads: dict[tuple[str, int], PDURead] | None = None):
    self.iccid = iccid
    self.reads = reads or {}
    self.deleted = []

  def current_iccid(self) -> str:
    return self.iccid

  def scan(self) -> list[StoredPDU]:
    return []

  def read(self, storage: str, index: int) -> PDURead:
    return self.reads[(storage, index)]

  def delete(self, storage: str, index: int) -> bool:
    self.deleted.append((storage, index))
    return True


def queue_acknowledged(store: MessageStore, raw_pdu: str) -> str:
  digest = hashlib.sha256(bytes.fromhex(raw_pdu)).hexdigest()
  stored = StoredPDU("SM", 8, raw_pdu, digest)
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
