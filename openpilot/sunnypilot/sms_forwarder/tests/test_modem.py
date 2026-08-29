import json

from openpilot.sunnypilot.sms_forwarder.modem import ATResult, PDURead, SMSModem, pdu_digest
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


def test_scan_retries_when_lock_or_modem_is_busy() -> None:
  client = FakeClient({"AT+CMGF=0": [None]})
  assert SMSModem(client=client).scan() is None


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


def test_iccid_is_read_from_shared_modem_state(tmp_path) -> None:
  state = tmp_path / "modem"
  state.write_text(json.dumps({"iccid": "8912345678901234567"}))
  assert SMSModem(client=FakeClient({}), state_path=str(state)).current_iccid() == "8912345678901234567"
  state.write_text(json.dumps({"iccid": "+invalid"}))
  assert SMSModem(client=FakeClient({}), state_path=str(state)).current_iccid() == ""
