# Comma 4 SIM message forwarder

This service reads human-readable inbound SMS messages from the Comma 4 modem, stores them durably, and forwards them to the configured `API_HOST`. It runs automatically on Comma 4, both onroad and offroad. There is no setting or Params key.

The service deliberately does not modify the main modem implementation or enable unsolicited modem notifications. It shares `/dev/shm/modem.lock`, polls `SM` and `ME` storage in PDU mode, and keeps both the modem slot and its SQLite copy until RTZ acknowledges the normalized message. The queue is stored at `/data/sms_forwarder/queue.sqlite3`.

Only SMS-DELIVER text using GSM 7-bit or UCS-2 is forwarded. Binary, MMS, provisioning, status reports, malformed PDUs, and incomplete multipart messages stay on the modem and are never uploaded or deleted.

## Migrating to a new Sunnypilot release

The package is isolated under `openpilot/sunnypilot/sms_forwarder/`. Copy it unchanged, then reapply its single integration hook:

1. Register the `sms_forwarder` Python process and its Comma 4 predicate in `openpilot/system/manager/process_config.py`.

Host tests exercise decoding, queuing, retry decisions, and safe acknowledgement cleanup. Receiving SMS over the carrier network and deleting acknowledged modem slots must also be validated on a physical Comma 4 with a personal SIM.
