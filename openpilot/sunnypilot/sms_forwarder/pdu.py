from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class PDUError(ValueError):
  pass


class UnsupportedPDU(PDUError):
  pass


GSM7_DEFAULT = (
  "@", "£", "$", "¥", "è", "é", "ù", "ì", "ò", "Ç", "\n", "Ø", "ø", "\r", "Å", "å",
  "Δ", "_", "Φ", "Γ", "Λ", "Ω", "Π", "Ψ", "Σ", "Θ", "Ξ", "\x1b", "Æ", "æ", "ß", "É",
  " ", "!", '"', "#", "¤", "%", "&", "'", "(", ")", "*", "+", ",", "-", ".", "/",
  "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ":", ";", "<", "=", ">", "?",
  "¡", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O",
  "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", "Ä", "Ö", "Ñ", "Ü", "§",
  "¿", "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o",
  "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z", "ä", "ö", "ñ", "ü", "à",
)

GSM7_EXTENSION = {
  0x0A: "\f",
  0x14: "^",
  0x28: "{",
  0x29: "}",
  0x2F: "\\",
  0x3C: "[",
  0x3D: "~",
  0x3E: "]",
  0x40: "|",
  0x65: "€",
}


@dataclass(frozen=True)
class ConcatInfo:
  reference: int
  reference_bits: int
  total: int
  sequence: int


@dataclass(frozen=True)
class DecodedPDU:
  sender: str
  sent_at: datetime
  body: str
  concat: ConcatInfo | None


def decode_sms_deliver(value: str) -> DecodedPDU:
  compact = "".join(value.split())
  if not compact or len(compact) % 2 != 0:
    raise PDUError("invalid PDU length")
  try:
    data = bytes.fromhex(compact)
  except ValueError as exc:
    raise PDUError("invalid PDU hex") from exc

  position = 0
  sca_length, position = _read_byte(data, position)
  position = _skip(data, position, sca_length)

  first_octet, position = _read_byte(data, position)
  if (first_octet & 0x03) != 0:
    raise UnsupportedPDU("not an SMS-DELIVER PDU")
  has_udh = bool(first_octet & 0x40)

  address_length, position = _read_byte(data, position)
  toa, position = _read_byte(data, position)
  address_bytes = (address_length + 1) // 2
  encoded_address, position = _read_bytes(data, position, address_bytes)
  sender = _decode_address(address_length, toa, encoded_address)

  pid, position = _read_byte(data, position)
  if pid != 0 and not 0x40 <= pid <= 0x47:
    raise UnsupportedPDU("non-human or provisioning protocol identifier")
  dcs, position = _read_byte(data, position)
  timestamp_bytes, position = _read_bytes(data, position, 7)
  sent_at = _decode_timestamp(timestamp_bytes)
  user_data_length, position = _read_byte(data, position)
  user_data = data[position:]

  alphabet = _decode_alphabet(dcs)
  header_length = 0
  concat = None
  if has_udh:
    if not user_data:
      raise PDUError("missing user data header")
    header_length = user_data[0] + 1
    if header_length > len(user_data):
      raise PDUError("truncated user data header")
    concat = _decode_concat_header(user_data[1:header_length])

  if alphabet == "gsm7":
    header_septets = (header_length * 8 + 6) // 7
    text_septets = user_data_length - header_septets
    if text_septets < 0:
      raise PDUError("invalid GSM-7 user data length")
    body = _decode_gsm7(user_data, text_septets, header_septets * 7)
  elif alphabet == "ucs2":
    if user_data_length > len(user_data) or header_length > user_data_length:
      raise PDUError("truncated UCS-2 user data")
    try:
      body = user_data[header_length:user_data_length].decode("utf-16-be")
    except UnicodeDecodeError as exc:
      raise PDUError("invalid UCS-2 user data") from exc
  else:
    raise UnsupportedPDU("binary or reserved SMS alphabet")

  return DecodedPDU(sender=sender, sent_at=sent_at, body=body, concat=concat)


def _decode_address(length: int, toa: int, encoded: bytes) -> str:
  if length == 0 or not (toa & 0x80):
    raise PDUError("invalid originator address")
  if (toa & 0x70) == 0x50:
    septets = length * 4 // 7
    return _decode_gsm7(encoded, septets, 0)
  digits = []
  for byte in encoded:
    low = byte & 0x0F
    high = (byte >> 4) & 0x0F
    if low > 9 or (high > 9 and high != 0x0F):
      raise PDUError("invalid numeric originator address")
    digits.append(str(low))
    if high != 0x0F:
      digits.append(str(high))
  value = "".join(digits)[:length]
  return "+" + value if (toa & 0x70) == 0x10 else value


def _decode_timestamp(value: bytes) -> datetime:
  if len(value) != 7:
    raise PDUError("invalid timestamp")
  fields = [_swapped_decimal(byte) for byte in value[:6]]
  tz_byte = value[6]
  negative = bool(tz_byte & 0x08)
  quarters = ((tz_byte & 0x07) * 10) + ((tz_byte >> 4) & 0x0F)
  offset = timedelta(minutes=quarters * 15)
  if negative:
    offset = -offset
  try:
    return datetime(2000 + fields[0], fields[1], fields[2], fields[3], fields[4], fields[5], tzinfo=timezone(offset))
  except ValueError as exc:
    raise PDUError("invalid timestamp fields") from exc


def _swapped_decimal(value: int) -> int:
  low = value & 0x0F
  high = (value >> 4) & 0x0F
  if low > 9 or high > 9:
    raise PDUError("invalid semi-octet decimal")
  return low * 10 + high


def _decode_alphabet(dcs: int) -> str:
  if (dcs & 0xC0) == 0x00:
    if dcs & 0x20:
      return "reserved"
    alphabet = (dcs >> 2) & 0x03
    return {0: "gsm7", 1: "binary", 2: "ucs2"}.get(alphabet, "reserved")
  if (dcs & 0xF0) == 0xF0:
    return "binary" if dcs & 0x04 else "gsm7"
  return "reserved"


def _decode_concat_header(header: bytes) -> ConcatInfo | None:
  position = 0
  result = None
  while position < len(header):
    if position + 2 > len(header):
      raise PDUError("truncated UDH element")
    identifier = header[position]
    length = header[position + 1]
    position += 2
    if position + length > len(header):
      raise PDUError("truncated UDH value")
    value = header[position:position + length]
    position += length
    if identifier in (0x04, 0x05, 0x70, 0x71):
      raise UnsupportedPDU("application-addressed or provisioning user data")
    if identifier == 0x00 and length == 3:
      result = ConcatInfo(reference=value[0], reference_bits=8, total=value[1], sequence=value[2])
    elif identifier == 0x08 and length == 4:
      result = ConcatInfo(reference=int.from_bytes(value[:2], "big"), reference_bits=16, total=value[2], sequence=value[3])
  if result is not None and (result.total < 2 or result.sequence < 1 or result.sequence > result.total):
    raise PDUError("invalid concatenation header")
  return result


def _decode_gsm7(data: bytes, count: int, bit_offset: int) -> str:
  required_bits = bit_offset + count * 7
  if required_bits > len(data) * 8:
    raise PDUError("truncated GSM-7 user data")
  packed = int.from_bytes(data, "little")
  septets = [packed >> (bit_offset + index * 7) & 0x7F for index in range(count)]
  result = []
  escaped = False
  for septet in septets:
    if escaped:
      result.append(GSM7_EXTENSION.get(septet, "�"))
      escaped = False
    elif septet == 0x1B:
      escaped = True
    else:
      result.append(GSM7_DEFAULT[septet])
  if escaped:
    result.append("�")
  return "".join(result)


def _read_byte(data: bytes, position: int) -> tuple[int, int]:
  value, position = _read_bytes(data, position, 1)
  return value[0], position


def _read_bytes(data: bytes, position: int, count: int) -> tuple[bytes, int]:
  end = position + count
  if count < 0 or end > len(data):
    raise PDUError("truncated PDU")
  return data[position:end], end


def _skip(data: bytes, position: int, count: int) -> int:
  _, position = _read_bytes(data, position, count)
  return position
