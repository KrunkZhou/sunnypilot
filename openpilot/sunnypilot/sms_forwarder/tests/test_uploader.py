from dataclasses import replace
from openpilot.sunnypilot.sms_forwarder.store import QueuedMessage
from openpilot.sunnypilot.sms_forwarder.uploader import ES256DeviceApi, RTZUploader


MESSAGE = QueuedMessage(
  message_id="a" * 64,
  sim_iccid="8912345678901234567",
  sender="+14165551234",
  sent_at="2026-08-29T01:15:30+00:00",
  received_at="2026-08-29T01:15:34+00:00",
  body="hello",
  parts=1,
)


class FakeStore:
  def __init__(self, messages: list[QueuedMessage]):
    self.messages = messages
    self.acknowledged = []

  def pending(self, limit: int) -> list[QueuedMessage]:
    return list(self.messages[:limit])

  def mark_acknowledged(self, message_ids: list[str]) -> None:
    self.acknowledged.extend(message_ids)


class FakeResponse:
  def __init__(self, status_code: int, payload: object):
    self.status_code = status_code
    self.payload = payload

  def json(self) -> object:
    return self.payload


class FakeApi:
  def __init__(self, response: FakeResponse):
    self.response = response
    self.calls = []

  def get_token(self) -> str:
    return "device-token"

  def post(self, endpoint: str, **kwargs) -> FakeResponse:
    self.calls.append((endpoint, kwargs))
    return self.response


def test_success_acknowledges_committed_ids() -> None:
  store = FakeStore([MESSAGE])
  api = FakeApi(FakeResponse(200, {"accepted": [MESSAGE.message_id]}))
  assert RTZUploader("dongle", store, api).upload_once() is True
  assert store.acknowledged == [MESSAGE.message_id]
  assert api.calls[0][0] == "v1/devices/dongle/sms"
  assert api.calls[0][1]["access_token"] == "device-token"


def test_non_200_and_invalid_acknowledgement_stay_queued() -> None:
  store = FakeStore([MESSAGE])
  assert RTZUploader("dongle", store, FakeApi(FakeResponse(500, {}))).upload_once() is False
  assert RTZUploader("dongle", store, FakeApi(FakeResponse(200, {"accepted": "wrong"}))).upload_once() is False
  assert store.acknowledged == []


def test_batch_is_limited_to_50_and_512_kib() -> None:
  messages = [replace(MESSAGE, message_id=f"{index:064x}", body="界" * 20_000) for index in range(60)]
  store = FakeStore(messages)
  accepted = [message.message_id for message in messages[:4]]
  api = FakeApi(FakeResponse(200, {"accepted": accepted}))
  assert RTZUploader("dongle", store, api).upload_once() is True
  payload = api.calls[0][1]["json"]
  assert 0 < len(payload["messages"]) <= 50
  assert len(payload["messages"]) == len(accepted)


def test_production_uploader_loads_only_the_es256_key(monkeypatch, tmp_path) -> None:
  key_directory = tmp_path / "comma"
  key_directory.mkdir()
  (key_directory / "id_rsa").write_text("legacy RSA private")
  (key_directory / "id_rsa.pub").write_text("legacy RSA public")
  (key_directory / "id_ecdsa").write_text("ECDSA private")
  (key_directory / "id_ecdsa.pub").write_text("ECDSA public")
  monkeypatch.setattr("openpilot.sunnypilot.sms_forwarder.uploader.Paths.persist_root", lambda: str(tmp_path))
  assert ES256DeviceApi.get_key_pair() == ("ES256", "ECDSA private", "ECDSA public")
