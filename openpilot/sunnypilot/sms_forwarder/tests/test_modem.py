import json

import pytest

from openpilot.sunnypilot.sms_forwarder.modem import ATResult, PDURead, SMSModem, StoredPDU, pdu_digest
from openpilot.sunnypilot.sms_forwarder.tests.helpers import sms_deliver_pdu


class FakeClient:
  def __init__(self, responses: dict[str, list[ATResult | None]]):
    self.responses = responses
    self.commands = []

  def command(self, command: str, timeout: float = 5.0) -> ATResult | None:
    self.commands.append(command)
    return self.responses[command].pop(0)


def test_scans_sm_and_me_in_pdu_mode() -> None:
  first = sms_deliver_pdu("from SIM")
  second = sms_deliver_pdu("from modem")
  client = FakeClient({
    "AT+CMGF=0": [ATResult(True, ())],
    'AT+CPMS="SM"': [ATResult(True, ())],
    "AT+CMGL=4": [
      ATResult(True, ("+CMGL: 7,1,,23", first)),
      ATResult(True, ("+CMGL: 9,1,,23", second)),
    ],
    'AT+CPMS="ME"': [ATResult(True, ())],
  })
  messages = SMSModem(client=client).scan()
  assert messages is not None
  assert [(message.storage, message.index) for message in messages] == [("SM", 7), ("ME", 9)]
  assert [message.digest for message in messages] == [pdu_digest(first), pdu_digest(second)]
  assert client.commands == [
    "AT+CMGF=0", 'AT+CPMS="SM"', "AT+CMGL=4",
    'AT+CPMS="ME"', "AT+CMGL=4",
  ]


@pytest.mark.parametrize("failure", [None, ATResult(False, ())])
def test_scan_retries_when_lock_or_modem_is_busy(failure) -> None:
  client = FakeClient({"AT+CMGF=0": [failure]})
  assert SMSModem(client=client).scan() is None


@pytest.mark.parametrize("failed_storage", ["SM", "ME"])
@pytest.mark.parametrize("failed_operation", ["select", "list"])
@pytest.mark.parametrize("failure", [None, ATResult(False, ())])
def test_scan_keeps_messages_when_other_storage_is_unavailable(failed_storage, failed_operation, failure) -> None:
  pdu = sms_deliver_pdu("available message")
  available_storage = "ME" if failed_storage == "SM" else "SM"
  responses = {"AT+CMGF=0": [ATResult(True, ())], "AT+CMGL=4": []}
  expected_commands = ["AT+CMGF=0"]
  for storage in ("SM", "ME"):
    select = f'AT+CPMS="{storage}"'
    expected_commands.append(select)
    if storage == failed_storage and failed_operation == "select":
      responses[select] = [failure]
    else:
      responses[select] = [ATResult(True, ())]
      responses["AT+CMGL=4"].append(failure if storage == failed_storage else ATResult(True, ("+CMGL: 7,1,,23", pdu)))
      expected_commands.append("AT+CMGL=4")
  client = FakeClient(responses)

  assert SMSModem(client=client).scan() == [StoredPDU(available_storage, 7, pdu, pdu_digest(pdu))]
  assert client.commands == expected_commands


@pytest.mark.parametrize("failed_operation", ["select", "list"])
@pytest.mark.parametrize("failure", [None, ATResult(False, ())])
def test_scan_retries_when_neither_storage_can_be_listed(failed_operation, failure) -> None:
  client = FakeClient({
    "AT+CMGF=0": [ATResult(True, ())],
    'AT+CPMS="SM"': [failure if failed_operation == "select" else ATResult(True, ())],
    'AT+CPMS="ME"': [failure if failed_operation == "select" else ATResult(True, ())],
    "AT+CMGL=4": [failure, failure],
  })

  assert SMSModem(client=client).scan() is None
  assert 'AT+CPMS="ME"' in client.commands


@pytest.mark.parametrize("failed_storage", ["SM", "ME"])
def test_scan_returns_empty_list_when_available_storage_is_empty(failed_storage) -> None:
  client = FakeClient({
    "AT+CMGF=0": [ATResult(True, ())],
    'AT+CPMS="SM"': [ATResult(True, ())],
    'AT+CPMS="ME"': [ATResult(True, ())],
    "AT+CMGL=4": [None if storage == failed_storage else ATResult(True, ()) for storage in ("SM", "ME")],
  })

  assert SMSModem(client=client).scan() == []


def test_read_and_delete_slot() -> None:
  pdu = sms_deliver_pdu("hello")
  client = FakeClient({
    "AT+CMGF=0": [ATResult(True, ())],
    'AT+CPMS="SM"': [ATResult(True, ()), ATResult(True, ())],
    "AT+CMGR=3": [ATResult(True, ("+CMGR: 1,,23", pdu))],
    "AT+CMGD=3": [ATResult(True, ())],
  })
  modem = SMSModem(client=client)
  assert modem.read("SM", 3) == PDURead("found", pdu)
  assert modem.delete("SM", 3)


@pytest.mark.parametrize("failed_operation", ["select", "delete"])
@pytest.mark.parametrize("failure, expected", [(None, None), (ATResult(False, ()), False)])
def test_delete_distinguishes_busy_modem_from_explicit_error(failed_operation, failure, expected) -> None:
  client = FakeClient({
    'AT+CPMS="SM"': [failure if failed_operation == "select" else ATResult(True, ())],
    "AT+CMGD=3": [failure],
  })

  assert SMSModem(client=client).delete("SM", 3) is expected
  assert client.commands == (['AT+CPMS="SM"'] if failed_operation == "select" else ['AT+CPMS="SM"', "AT+CMGD=3"])


def test_iccid_is_read_from_shared_modem_state(tmp_path) -> None:
  state = tmp_path / "modem"
  state.write_text(json.dumps({"iccid": "8912345678901234567"}))
  assert SMSModem(client=FakeClient({}), state_path=str(state)).current_iccid() == "8912345678901234567"
  state.write_text(json.dumps({"iccid": "+invalid"}))
  assert SMSModem(client=FakeClient({}), state_path=str(state)).current_iccid() == ""
