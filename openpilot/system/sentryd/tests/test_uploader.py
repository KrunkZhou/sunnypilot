import base64
import io
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import requests

from openpilot.common.hardware.hw import Paths
from openpilot.system.sentryd.store import MediaData, SentryStore
from openpilot.system.sentryd.uploader import MAX_ES256_PUBLIC_KEY_BYTES, RequestsSentryTransport, SentryUploader


NOW = datetime(2026, 9, 4, tzinfo=UTC).isoformat()
JPEG = b"\xff\xd8image\xff\xd9"


class Response:
  def __init__(self, status_code, payload, headers=None):
    self.status_code = status_code
    self.payload = payload
    self.headers = headers or {}
    self.raw = io.BytesIO(json.dumps(payload).encode())
    self.closed = False

  def json(self):
    return self.payload

  def close(self):
    self.closed = True


class Transport:
  def __init__(self, response):
    self.response = response
    self.calls = []

  def post_event(self, dongle_id, revision, files):
    self.calls.append((dongle_id, revision, set(files)))
    if isinstance(self.response, Exception):
      raise self.response
    return self.response


class Api:
  api_host = "https://rtz.example"
  user_agent = "test-"

  def __init__(self, token):
    self.token = token
    self.token_calls = []

  def get_token(self, payload_extra=None, expiry_hours=1):
    self.token_calls.append((payload_extra, expiry_hours))
    return self.token

  @staticmethod
  def remove_non_ascii_chars(value):
    return value


class Session:
  def __init__(self, response=None):
    self.response = response or Response(500, {})
    self.calls = []

  def post(self, url, **kwargs):
    self.calls.append((url, kwargs))
    return self.response


def queued_store(tmp_path, with_media=True):
  store = SentryStore(tmp_path / "outbox.sqlite3")
  event_id = str(uuid4())
  store.begin_revision(
    event_id=event_id, revision=1, kind="warning", source="motion",
    episode_started_at=NOW, detected_at=NOW, message="Movement detected while parked.",
  )
  media = {"wide": MediaData(JPEG, 10, 10)} if with_media else {}
  omissions = {"cabin": "camera_unavailable"} if with_media else {
    "wide": "capture_failed", "cabin": "capture_failed",
  }
  store.finish_capture(event_id, 1, media, omissions)
  return store, event_id


def ack_for(store):
  queued = store.next_pending(0)
  return {
    "event_id": queued.event_id,
    "revision": queued.revision,
    "media": [{"role": item.role, "sha256": item.sha256} for item in queued.media],
    "deleted": False,
  }


def test_default_transport_preserves_base_api_registered_key_preference(tmp_path, monkeypatch) -> None:
  persist = tmp_path / "persist"
  key_dir = persist / "comma"
  key_dir.mkdir(parents=True)
  for name in ("id_rsa", "id_rsa.pub", "id_ecdsa", "id_ecdsa.pub"):
    (key_dir / name).write_text(name)
  monkeypatch.setattr(Paths, "persist_root", lambda: str(persist))

  transport = RequestsSentryTransport("dongle", session=Session())

  assert transport.api.jwt_algorithm == "RS256"
  assert transport.es256_api.jwt_algorithm == "ES256"
  assert transport.es256_public_key_path == key_dir / "id_ecdsa.pub"


def test_requests_transport_sends_registered_and_es256_device_proofs(tmp_path) -> None:
  public_key = b"-----BEGIN PUBLIC KEY-----\nP256 test key\n-----END PUBLIC KEY-----\n"
  public_key_path = tmp_path / "id_ecdsa.pub"
  public_key_path.write_bytes(public_key)
  primary_api = Api("registered-key-jwt")
  es256_api = Api("es256-jwt")
  session = Session()
  store, _ = queued_store(tmp_path)
  revision = store.next_pending(0)
  transport = RequestsSentryTransport(
    "dongle", api=primary_api, es256_api=es256_api,
    es256_public_key_path=public_key_path, session=session,
  )

  transport.post_event("dongle", revision, {"wide": io.BytesIO(JPEG)})

  assert primary_api.token_calls == [(None, 1)]
  assert es256_api.token_calls == [({"sentry": True}, 1)]
  url, request = session.calls[0]
  assert url == "https://rtz.example/v1/devices/dongle/sentry-events"
  assert request["headers"]["Authorization"] == "JWT registered-key-jwt"
  assert request["headers"]["X-RTZ-Sentry-ES256-Token"] == "es256-jwt"
  encoded_key = request["headers"]["X-RTZ-Sentry-ES256-Public-Key"]
  assert base64.b64decode(encoded_key, validate=True) == public_key
  assert len(encoded_key) <= 5464
  assert request["stream"] is True and request["allow_redirects"] is False


@pytest.mark.parametrize("unsafe", ("oversized", "symlink"))
def test_requests_transport_rejects_unsafe_es256_public_key(tmp_path, unsafe) -> None:
  public_key_path = tmp_path / "id_ecdsa.pub"
  if unsafe == "oversized":
    public_key_path.write_bytes(b"x" * (MAX_ES256_PUBLIC_KEY_BYTES + 1))
  else:
    target = tmp_path / "actual.pub"
    target.write_bytes(b"public key")
    public_key_path.symlink_to(target)
  session = Session()
  transport = RequestsSentryTransport(
    "dongle", api=Api("primary"), es256_api=Api("es256"),
    es256_public_key_path=public_key_path, session=session,
  )
  store, _ = queued_store(tmp_path / "queue")

  with pytest.raises(OSError, match="public key"):
    transport.post_event("dongle", store.next_pending(0), {})
  assert session.calls == []


def test_missing_es256_proof_is_durably_retried(tmp_path) -> None:
  store, event_id = queued_store(tmp_path)
  session = Session()
  transport = RequestsSentryTransport(
    "dongle", api=Api("primary"), es256_api=Api("es256"),
    es256_public_key_path=tmp_path / "missing.pub", session=session,
  )

  assert SentryUploader("dongle", store, transport, random_uniform=lambda *_: 0).upload_once(now=100) is False
  row = store.connection.execute(
    "SELECT state, attempts, next_attempt_at, last_error FROM revisions WHERE event_id=?", (event_id,)
  ).fetchone()
  assert (row["state"], row["attempts"], row["next_attempt_at"]) == ("ready", 1, 105.0)
  assert "public key is unavailable" in row["last_error"]
  assert session.calls == []


def test_es256_signing_failure_is_durably_retried(tmp_path) -> None:
  class SigningFailure(Exception):
    pass

  class FailingApi(Api):
    def get_token(self, payload_extra=None, expiry_hours=1):
      raise SigningFailure("private signing detail")

  public_key_path = tmp_path / "id_ecdsa.pub"
  public_key_path.write_bytes(b"public key")
  store, event_id = queued_store(tmp_path)
  session = Session()
  transport = RequestsSentryTransport(
    "dongle", api=Api("primary"), es256_api=FailingApi("unused"),
    es256_public_key_path=public_key_path, session=session,
  )

  assert SentryUploader("dongle", store, transport, random_uniform=lambda *_: 0).upload_once(now=100) is False
  row = store.connection.execute(
    "SELECT state, attempts, next_attempt_at, last_error FROM revisions WHERE event_id=?", (event_id,)
  ).fetchone()
  assert (row["state"], row["attempts"], row["next_attempt_at"]) == ("ready", 1, 105.0)
  assert row["last_error"] == "Sentry device authentication token could not be generated"
  assert "private signing detail" not in row["last_error"]
  assert session.calls == []


def test_success_requires_matching_ack_and_removes_media(tmp_path) -> None:
  store, _ = queued_store(tmp_path)
  response = Response(201, ack_for(store))
  transport = Transport(response)
  assert SentryUploader("dongle", store, transport).upload_once(now=100) is True
  assert transport.calls[0][0] == "dongle"
  assert transport.calls[0][2] == {"wide"}
  assert store.stats().media_bytes == 0
  assert response.closed


def test_network_and_transient_statuses_remain_queued_with_backoff(tmp_path) -> None:
  store, event_id = queued_store(tmp_path)
  uploader = SentryUploader("dongle", store, Transport(requests.ConnectionError("offline")), random_uniform=lambda *_: 0)
  assert uploader.upload_once(now=100) is False
  row = store.connection.execute(
    "SELECT state, attempts, next_attempt_at FROM revisions WHERE event_id=?", (event_id,)
  ).fetchone()
  assert tuple(row) == ("ready", 1, 105.0)

  with store.connection:
    store.connection.execute("UPDATE revisions SET next_attempt_at=0 WHERE event_id=?", (event_id,))
  uploader = SentryUploader("dongle", store, Transport(Response(429, {}, {"Retry-After": "30"})), random_uniform=lambda *_: 0)
  assert uploader.upload_once(now=200) is False
  assert store.connection.execute(
    "SELECT attempts, next_attempt_at FROM revisions WHERE event_id=?", (event_id,)
  ).fetchone()[1] == 230.0


def test_retry_delay_starts_after_slow_request_finishes(tmp_path) -> None:
  store, event_id = queued_store(tmp_path)
  times = iter((100.0, 140.0))
  uploader = SentryUploader(
    "dongle", store, Transport(requests.ConnectionError("offline")),
    random_uniform=lambda *_: 0, wall_clock=lambda: next(times),
  )
  assert uploader.upload_once() is False
  assert store.connection.execute(
    "SELECT next_attempt_at FROM revisions WHERE event_id=?", (event_id,)
  ).fetchone()[0] == 145.0


def test_terminal_4xx_is_retained_for_manual_retry(tmp_path) -> None:
  store, event_id = queued_store(tmp_path, with_media=False)
  response = Response(422, {})
  assert SentryUploader("dongle", store, Transport(response)).upload_once(now=100) is False
  assert store.connection.execute("SELECT state FROM revisions WHERE event_id=?", (event_id,)).fetchone()[0] == "terminal"
  assert store.retry_terminal() == 1
  assert response.closed


def test_nonstandard_600_response_is_not_treated_as_5xx_retry(tmp_path) -> None:
  store, event_id = queued_store(tmp_path, with_media=False)
  assert SentryUploader("dongle", store, Transport(Response(600, {}))).upload_once(now=100) is False
  assert store.connection.execute(
    "SELECT state FROM revisions WHERE event_id=?", (event_id,)
  ).fetchone()[0] == "terminal"


def test_mismatched_ack_is_retried_and_tombstone_ack_is_accepted(tmp_path) -> None:
  store, event_id = queued_store(tmp_path)
  bad = ack_for(store)
  bad["event_id"] = str(uuid4())
  assert SentryUploader("dongle", store, Transport(Response(200, bad)), random_uniform=lambda *_: 0).upload_once(now=100) is False
  with store.connection:
    store.connection.execute("UPDATE revisions SET next_attempt_at=0 WHERE event_id=?", (event_id,))
  deleted = {"event_id": event_id, "revision": 1, "media": [], "deleted": True}
  assert SentryUploader("dongle", store, Transport(Response(200, deleted))).upload_once(now=200) is True


def test_oversized_ack_is_bounded_retried_and_closed(tmp_path) -> None:
  store, event_id = queued_store(tmp_path)
  response = Response(200, {})
  response.raw = io.BytesIO(b"{" + b" " * (64 * 1024) + b"}")
  assert SentryUploader("dongle", store, Transport(response), random_uniform=lambda *_: 0).upload_once(now=100) is False
  row = store.connection.execute(
    "SELECT state, next_attempt_at, last_error FROM revisions WHERE event_id=?", (event_id,)
  ).fetchone()
  assert row["state"] == "ready" and row["next_attempt_at"] == 105
  assert "too large" in row["last_error"]
  assert response.closed
