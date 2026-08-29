from __future__ import annotations

import json

import requests

from openpilot.common.api.base import BaseApi
from openpilot.common.api.comma_connect import API_HOST
from openpilot.common.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot.sms_forwarder.store import MessageStore


class ES256DeviceApi(BaseApi):
  def __init__(self, dongle_id: str):
    super().__init__(dongle_id, API_HOST)

  @staticmethod
  def get_key_pair() -> tuple[str, str, str] | tuple[None, None, None]:
    private_path = Paths.persist_root() + "/comma/id_ecdsa"
    public_path = private_path + ".pub"
    try:
      with open(private_path) as private, open(public_path) as public:
        return "ES256", private.read(), public.read()
    except OSError:
      return None, None, None


class RTZUploader:
  def __init__(self, dongle_id: str, store: MessageStore, api: BaseApi | None = None):
    self.dongle_id = dongle_id
    self.store = store
    self.api = api or ES256DeviceApi(dongle_id)

  def upload_once(self) -> bool | None:
    messages = self.store.pending(50)
    if not messages:
      return None
    payload = {"messages": [message.api_dict() for message in messages]}
    while len(json.dumps(payload, allow_nan=False).encode()) > 512 * 1024:
      messages.pop()
      payload["messages"].pop()
    if not messages:
      cloudlog.error("Queued SIM message exceeds the RTZ request limit")
      return False
    message_ids = {message.message_id for message in messages}
    try:
      response = self.api.post(
        f"v1/devices/{self.dongle_id}/sms",
        timeout=10,
        access_token=self.api.get_token(),
        json=payload,
      )
    except (AssertionError, OSError, requests.RequestException):
      cloudlog.exception("SIM message upload failed")
      return False
    if response.status_code != 200:
      cloudlog.warning("SIM message upload rejected with status %d", response.status_code)
      return False
    try:
      payload = response.json()
      accepted_value = payload.get("accepted") if isinstance(payload, dict) else None
      if not isinstance(accepted_value, list) or not all(isinstance(value, str) for value in accepted_value):
        raise ValueError("invalid acknowledgement")
      accepted = [value for value in accepted_value if value in message_ids]
    except ValueError:
      cloudlog.warning("SIM message upload returned an invalid acknowledgement")
      return False
    self.store.mark_acknowledged(accepted)
    return len(accepted) == len(messages)
