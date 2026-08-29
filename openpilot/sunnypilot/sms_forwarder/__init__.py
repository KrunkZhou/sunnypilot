"""Durable inbound SMS forwarding for comma four personal SIMs."""


def is_supported_device(device_type: str) -> bool:
  return device_type == "mici"
