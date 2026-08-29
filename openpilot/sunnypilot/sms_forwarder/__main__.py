from __future__ import annotations

import random
import signal
import threading
import time
from datetime import UTC, datetime

from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.sms_forwarder.modem import SMSModem, pdu_digest
from openpilot.sunnypilot.sms_forwarder.pdu import PDUError, UnsupportedPDU, decode_sms_deliver
from openpilot.sunnypilot.sms_forwarder.store import MessageStore, default_store_path
from openpilot.sunnypilot.sms_forwarder.uploader import RTZUploader


POLL_INTERVAL = 15.0
INITIAL_RETRY = 30.0
MAX_RETRY = 15 * 60.0


class RetryBackoff:
  def __init__(self):
    self.current = INITIAL_RETRY

  def failure_delay(self) -> float:
    delay = min(self.current + random.uniform(0, self.current * 0.25), MAX_RETRY)
    self.current = min(self.current * 2, MAX_RETRY)
    return delay

  def reset(self) -> None:
    self.current = INITIAL_RETRY


class SMSForwarder:
  def __init__(self, dongle_id: str, store: MessageStore, modem: SMSModem, uploader: RTZUploader):
    self.dongle_id = dongle_id
    self.store = store
    self.modem = modem
    self.uploader = uploader

  def scan(self) -> bool:
    sim_iccid = self.modem.current_iccid()
    if not sim_iccid:
      return False
    stored_messages = self.modem.scan()
    if stored_messages is None:
      return False
    received_at = datetime.now(UTC)
    added = 0
    for stored in stored_messages:
      try:
        decoded = decode_sms_deliver(stored.raw_pdu)
      except UnsupportedPDU:
        continue
      except PDUError:
        cloudlog.warning("Ignoring malformed stored SIM message")
        continue
      if self.store.add_pdu(stored, decoded, sim_iccid, received_at):
        added += 1
    if added:
      cloudlog.info("Saved %d readable SIM message parts", added)
    return True

  def cleanup(self) -> None:
    current_iccid = self.modem.current_iccid()
    if not current_iccid:
      return
    for message in self.store.cleanup_messages():
      if message.sim_iccid != current_iccid:
        continue
      retry = False
      for location in message.locations:
        read = self.modem.read(location.storage, location.index)
        if read.status == "retry":
          retry = True
          break
        if read.status == "missing":
          self.store.discard_location(location)
          continue
        try:
          matches = pdu_digest(read.raw_pdu) == location.digest
        except ValueError:
          matches = False
        if not matches:
          # The slot was reused. Forget our stale reference but never delete the new message.
          self.store.discard_location(location)
        elif self.modem.delete(location.storage, location.index):
          self.store.discard_location(location)
        else:
          retry = True
          break
      if not retry and self.store.finish_cleanup(message.message_id):
        cloudlog.info("Removed acknowledged SIM message from the local queue")

  def run(self, stop_event: threading.Event) -> None:
    next_scan = 0.0
    next_upload = 0.0
    next_cleanup = 0.0
    retry_backoff = RetryBackoff()
    while not stop_event.is_set():
      now = time.monotonic()
      if now >= next_scan:
        self.scan()
        next_scan = now + POLL_INTERVAL
      if now >= next_cleanup:
        self.cleanup()
        next_cleanup = now + POLL_INTERVAL
      if now >= next_upload:
        success = self.uploader.upload_once()
        if success is False:
          next_upload = now + retry_backoff.failure_delay()
        else:
          retry_backoff.reset()
          next_upload = now + (1.0 if success else POLL_INTERVAL)
      deadline = min(next_scan, next_cleanup, next_upload)
      stop_event.wait(max(0.1, min(1.0, deadline - time.monotonic())))


def main() -> None:
  from openpilot.common.params import Params

  stop_event = threading.Event()
  signal.signal(signal.SIGINT, lambda *_: stop_event.set())
  signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
  dongle_id = Params().get("DongleId")
  if not isinstance(dongle_id, str) or not dongle_id:
    raise RuntimeError("SIM message forwarder requires a registered DongleId")
  store = MessageStore(default_store_path())
  try:
    modem = SMSModem()
    SMSForwarder(dongle_id, store, modem, RTZUploader(dongle_id, store)).run(stop_event)
  finally:
    store.close()


if __name__ == "__main__":
  main()
