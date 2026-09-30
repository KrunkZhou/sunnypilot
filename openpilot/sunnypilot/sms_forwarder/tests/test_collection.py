import json
from unittest.mock import Mock

import pytest

from openpilot.sunnypilot.sms_forwarder.__main__ import SMSForwarder
from openpilot.sunnypilot.sms_forwarder.modem import ATClient, ATResult, SMSModem
from openpilot.sunnypilot.sms_forwarder.store import MessageStore
from openpilot.sunnypilot.sms_forwarder.tests.helpers import sms_deliver_pdu
from openpilot.sunnypilot.sms_forwarder.uploader import RTZUploader


@pytest.mark.parametrize("failed_storage", ["SM", "ME"])
def test_partial_scan_is_durable_and_uploadable_before_other_storage_recovers(tmp_path, failed_storage) -> None:
  iccid = "8912345678901234567"
  state = tmp_path / "modem"
  state.write_text(json.dumps({"iccid": iccid}))
  pdu = sms_deliver_pdu("available message")
  response = ATResult(True, ("+CMGL: 7,1,,23", pdu))
  client = Mock(spec=ATClient)
  client.command.side_effect = [
    ATResult(True, ()),
    ATResult(True, ()), None if failed_storage == "SM" else response,
    ATResult(True, ()), None if failed_storage == "ME" else response,
  ] * 2
  modem = SMSModem(client=client, state_path=str(state))
  queue_path = str(tmp_path / "queue.sqlite3")
  store = MessageStore(queue_path)
  try:
    forwarder = SMSForwarder("dongle", store, modem, Mock())
    assert forwarder.scan()
    assert store.counts() == (1, 1, 0)
    queued = store.pending()
    assert queued[0].body == "available message"
    assert queued[0].sim_iccid == iccid
  finally:
    store.close()

  # A restart and another partial scan must preserve, rather than duplicate, the queued message.
  store = MessageStore(queue_path)
  try:
    assert store.pending() == queued
    assert SMSForwarder("dongle", store, modem, Mock()).scan()
    assert store.pending() == queued
    api = Mock()
    api.post.return_value.status_code = 200
    api.post.return_value.json.return_value = {"accepted": [queued[0].message_id]}
    assert RTZUploader("dongle", store, api).upload_once()
    assert api.post.call_args.kwargs["json"] == {"messages": [queued[0].api_dict()]}
    assert store.counts() == (1, 0, 1)
    assert all(not call.args[0].startswith("AT+CMGD") for call in client.command.call_args_list)
  finally:
    store.close()
