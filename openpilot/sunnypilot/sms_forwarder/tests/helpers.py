from __future__ import annotations

from openpilot.sunnypilot.sms_forwarder.pdu import GSM7_DEFAULT, GSM7_EXTENSION


_GSM7_DEFAULT_REVERSE = {value: index for index, value in enumerate(GSM7_DEFAULT)}
_GSM7_EXTENSION_REVERSE = {value: index for index, value in GSM7_EXTENSION.items()}


def sms_deliver_pdu(
  body: str, *, sender: str = "+14165551234", alphabet: str = "gsm7", udh: bytes = b"", pid: int = 0, dcs_override: int | None = None,
) -> str:
  first_octet = 0x40 if udh else 0
  address = _encode_address(sender)
  timestamp = bytes.fromhex("62808212510300")  # 2026-08-28 21:15:30 +00:00

  if alphabet == "gsm7":
    septets = _encode_gsm7(body)
    header_septets = (len(udh) * 8 + 6) // 7
    bit_offset = header_septets * 7
    packed = int.from_bytes(udh, "little")
    for index, septet in enumerate(septets):
      packed |= septet << (bit_offset + index * 7)
    byte_count = (bit_offset + len(septets) * 7 + 7) // 8
    user_data = packed.to_bytes(byte_count, "little")
    user_data_length = header_septets + len(septets)
    dcs = 0x00
  elif alphabet == "ucs2":
    user_data = udh + body.encode("utf-16-be")
    user_data_length = len(user_data)
    dcs = 0x08
  elif alphabet == "binary":
    user_data = udh + body.encode()
    user_data_length = len(user_data)
    dcs = 0x04
  else:
    raise ValueError(alphabet)

  tpdu = bytes([first_octet, len(sender.removeprefix("+")), 0x91 if sender.startswith("+") else 0x81])
  tpdu += address + bytes([pid, dcs if dcs_override is None else dcs_override]) + timestamp + bytes([user_data_length]) + user_data
  return (b"\x00" + tpdu).hex().upper()


def concat_8(reference: int, total: int, sequence: int) -> bytes:
  return bytes([5, 0, 3, reference, total, sequence])


def concat_16(reference: int, total: int, sequence: int) -> bytes:
  return bytes([6, 8, 4, reference >> 8, reference & 0xFF, total, sequence])


def _encode_address(sender: str) -> bytes:
  digits = sender.removeprefix("+")
  if len(digits) % 2:
    digits += "F"
  return bytes(int(digits[index + 1] + digits[index], 16) for index in range(0, len(digits), 2))


def _encode_gsm7(body: str) -> list[int]:
  result = []
  for character in body:
    if character in _GSM7_DEFAULT_REVERSE and character != "\x1b":
      result.append(_GSM7_DEFAULT_REVERSE[character])
    elif character in _GSM7_EXTENSION_REVERSE:
      result.extend((0x1B, _GSM7_EXTENSION_REVERSE[character]))
    else:
      raise ValueError(f"not a GSM-7 character: {character!r}")
  return result
