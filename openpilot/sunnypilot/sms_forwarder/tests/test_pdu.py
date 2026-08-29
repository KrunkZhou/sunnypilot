from datetime import datetime

import pytest

from openpilot.sunnypilot.sms_forwarder.pdu import PDUError, UnsupportedPDU, decode_sms_deliver
from openpilot.sunnypilot.sms_forwarder.tests.helpers import concat_8, concat_16, sms_deliver_pdu


def test_gsm7_and_extension_characters() -> None:
  body = "Hello ^{}\\[~]|€"
  decoded = decode_sms_deliver(sms_deliver_pdu(body))
  assert decoded.sender == "+14165551234"
  assert decoded.body == body
  assert decoded.sent_at == datetime.fromisoformat("2026-08-28T21:15:30+00:00")
  assert decoded.concat is None


def test_ucs2_unicode() -> None:
  assert decode_sms_deliver(sms_deliver_pdu("你好, café 🚗", alphabet="ucs2")).body == "你好, café 🚗"


@pytest.mark.parametrize(("header", "reference", "bits"), [
  (concat_8(0x52, 3, 2), 0x52, 8),
  (concat_16(0x1234, 3, 2), 0x1234, 16),
])
def test_multipart_headers(header: bytes, reference: int, bits: int) -> None:
  decoded = decode_sms_deliver(sms_deliver_pdu("part two", udh=header))
  assert decoded.body == "part two"
  assert decoded.concat is not None
  assert (decoded.concat.reference, decoded.concat.reference_bits) == (reference, bits)
  assert (decoded.concat.total, decoded.concat.sequence) == (3, 2)


def test_ucs2_multipart_header() -> None:
  decoded = decode_sms_deliver(sms_deliver_pdu("第二部分", alphabet="ucs2", udh=concat_16(0xCAFE, 2, 2)))
  assert decoded.body == "第二部分"
  assert decoded.concat is not None and decoded.concat.reference == 0xCAFE


def test_binary_and_status_report_are_unsupported() -> None:
  with pytest.raises(UnsupportedPDU):
    decode_sms_deliver(sms_deliver_pdu("binary", alphabet="binary"))
  status_report = bytearray.fromhex(sms_deliver_pdu("hello"))
  status_report[1] = 0x02
  with pytest.raises(UnsupportedPDU):
    decode_sms_deliver(status_report.hex())


def test_provisioning_protocol_identifier_is_unsupported() -> None:
  with pytest.raises(UnsupportedPDU):
    decode_sms_deliver(sms_deliver_pdu("SIM data download", pid=0x7F))


@pytest.mark.parametrize("dcs", [0x20, 0xD0, 0xE0])
def test_compressed_and_message_waiting_data_is_unsupported(dcs: int) -> None:
  with pytest.raises(UnsupportedPDU):
    decode_sms_deliver(sms_deliver_pdu("network message", dcs_override=dcs))


def test_application_port_addressing_is_unsupported() -> None:
  with pytest.raises(UnsupportedPDU):
    decode_sms_deliver(sms_deliver_pdu("WAP push", udh=bytes([4, 5, 2, 0x0B, 0x84])))


@pytest.mark.parametrize("value", ["", "0", "ZZ", "00000B91"])
def test_malformed_pdu(value: str) -> None:
  with pytest.raises(PDUError):
    decode_sms_deliver(value)
