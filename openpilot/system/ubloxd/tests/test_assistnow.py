"""Host-only tests; no Params, cereal, serial port, network, or device required."""
import sys
import importlib.util
import threading
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpilot.system.ubloxd.assistnow import (
  AssistNowClient, MAX_ASSIST_BYTES, MGAInjector, PROTOCOL_HEADER, PROTOCOL_VERSION, ReceiverProbe,
  RetryLater, Session, UARTAssistance, UbxStream, download, mga_ack_result, parse_assistance, response_error, retry_delay, ubx_message,
)


IDENTITY = {
  "UBX-SEC-UNIQID": ubx_message(b"\x27\x03", b"\x02\x00\x00\x00\x11\x22\x33\x44\x55\x66").hex(),
  "UBX-MON-VER": ubx_message(b"\x0a\x04", b"EXT CORE 4.04".ljust(30, b"\x00") + b"00190000\x00\x00" + b"MOD=NEO-M9N".ljust(30, b"\x00")).hex(),
}
ASSIST = ubx_message(b"\x13\x00", b"\x01\x00\x06\x00" + bytes(64))
TIME_UTC = ubx_message(b"\x13\x40", bytes((0x10, 0, 0, 0x80, 0xea, 0x07, 9, 6, 12, 0, 0, 0)) + bytes(12))
NAVIGATION = ubx_message(b"\x01\x07", bytes(92))


def acknowledgement(sent=ASSIST, accepted=True, version=0, info=None):
  return ubx_message(b"\x13\x60", bytes((int(accepted), version, int(not accepted) if info is None else info, sent[3])) + sent[6:10])


def response(status=200, data=b"", headers=None):
  return SimpleNamespace(status_code=status, content=data, headers=headers or {})


class FakeAPI:
  def __init__(self, responses):
    self.responses = list(responses)
    self.calls = []

  def get_token(self):
    return "test-device-jwt"

  def request(self, method, endpoint, **kwargs):
    self.calls.append((method, endpoint, kwargs))
    result = self.responses.pop(0)
    if isinstance(result, Exception):
      raise result
    return result

  def get(self, endpoint, **kwargs):
    return self.request("GET", endpoint, **kwargs)

  def post(self, endpoint, **kwargs):
    return self.request("POST", endpoint, **kwargs)


class TestUBX(unittest.TestCase):
  def test_assistance_body_validation(self):
    self.assertEqual(parse_assistance(ASSIST + ASSIST), [ASSIST, ASSIST])
    malformed = [b"", b"x" + ASSIST, ASSIST[:-1], ASSIST + b"x", ASSIST[:-1] + bytes((ASSIST[-1] ^ 1,)),
                 NAVIGATION, acknowledgement(), ubx_message(b"\x13\x00"), b"x" * (MAX_ASSIST_BYTES + 1)]
    for value in malformed:
      with self.subTest(size=len(value)), self.assertRaises(ValueError):
        parse_assistance(value)

  def test_stream_fragmentation_and_resynchronization(self):
    stream = UbxStream()
    data = b"garbage" + ASSIST[:-1] + b"\xff" + b"\xb5\x62\x01\x07\xff\xff" + NAVIGATION + ASSIST
    frames = []
    for byte in data:
      frames.extend(stream.feed(bytes((byte,))))
    self.assertEqual(frames, [NAVIGATION, ASSIST])
    self.assertEqual(stream.buffer, b"")

  def test_probe_poll_deadline_and_full_frames(self):
    probe = ReceiverProbe(10.0)
    self.assertEqual(len(probe.poll(10.0)), 2)
    self.assertEqual(probe.poll(10.1), [])
    probe.feed(bytes.fromhex(IDENTITY["UBX-SEC-UNIQID"]))
    self.assertEqual(probe.poll(10.5), [ubx_message(b"\x0a\x04")])
    probe.feed(NAVIGATION)
    probe.feed(bytes.fromhex(IDENTITY["UBX-MON-VER"]))
    self.assertTrue(probe.complete)
    self.assertEqual(probe.messages, IDENTITY)
    self.assertEqual(probe.poll(10.6), [])
    self.assertEqual(ReceiverProbe(10.0).poll(11.0), [])

  def test_probe_rejects_malformed_identity(self):
    probe = ReceiverProbe(0.0)
    for frame in (ubx_message(b"\x27\x03", bytes(9)), ubx_message(b"\x27\x03", b"\x02" + bytes(6)),
                  ubx_message(b"\x27\x03", b"\x02\x00\x00\x00\x11\x22\x33\x44\x55"),
                  ubx_message(b"\x27\x03", b"\x01\x00\x00\x00" + b"\xff" * 5),
                  ubx_message(b"\x0a\x04", bytes(41)), ubx_message(b"\x0a\x04", bytes(39)),
                  bytes.fromhex(IDENTITY["UBX-MON-VER"])[:-1] + b"\xff"):
      probe.feed(frame)
    self.assertEqual(probe.messages, {})
    probe.feed(ubx_message(b"\x27\x03", b"\x01\x00\x00\x00\x11\x22\x33\x44\x55"))
    self.assertIn("UBX-SEC-UNIQID", probe.messages)

  def test_ack_checks_status_checksum_and_correlation(self):
    self.assertTrue(mga_ack_result(acknowledgement(), ASSIST))
    for frame in (acknowledgement(accepted=False), acknowledgement(version=1), acknowledgement(info=5),
                  ubx_message(b"\x05\x00", ASSIST[2:4])):
      self.assertFalse(mga_ack_result(frame, ASSIST))
    other = ubx_message(b"\x13\x00", b"\x01\x00\x07\x00" + bytes(64))
    for frame in (acknowledgement(other), NAVIGATION, ubx_message(b"\x13\x60", bytes(7)),
                  acknowledgement()[:-1] + bytes((acknowledgement()[-1] ^ 1,)), ubx_message(b"\x05\x00", b"\x06\x00")):
      self.assertIsNone(mga_ack_result(frame, ASSIST))

  def test_injector_bounded_retries(self):
    injector = MGAInjector([ASSIST], 0.0)
    injector.feed(acknowledgement())  # Ignore acknowledgements before sending.
    self.assertEqual(injector.poll(0.0), ASSIST)
    self.assertIsNone(injector.poll(0.49))
    self.assertEqual(injector.poll(0.5), ASSIST)
    self.assertIsNone(injector.poll(1.0))
    self.assertTrue(injector.done)
    self.assertEqual(injector.error, "assistance acknowledgement timed out")

  def test_absolute_time_is_never_replayed(self):
    for time_type in (0x10, 0x11):
      message = ubx_message(b"\x13\x40", bytes((time_type,)) + TIME_UTC[7:-2])
      injector = MGAInjector([message, ASSIST], 0.0)
      self.assertEqual(injector.poll(0.0), message)
      self.assertIsNone(injector.poll(0.5))
      self.assertTrue(injector.done)
      self.assertEqual(injector.index, 0)
      self.assertIsNotNone(injector.error)

  def test_provider_time_and_original_byte_order_are_preserved(self):
    original = TIME_UTC + ASSIST
    injector = MGAInjector(parse_assistance(original), 0.0)
    sent = [injector.poll(0.0)]
    injector.feed(acknowledgement(TIME_UTC))
    sent.append(injector.poll(0.1))
    self.assertEqual(b"".join(sent), original)

  def test_injector_nack_stops_and_success_advances(self):
    second = ubx_message(b"\x13\x00", b"\x01\x00\x07\x00" + bytes(64))
    injector = MGAInjector([ASSIST, second], 0.0)
    self.assertEqual(injector.poll(0.0), ASSIST)
    injector.feed(acknowledgement())
    self.assertEqual(injector.poll(0.1), second)
    injector.feed(acknowledgement())  # A duplicate must not acknowledge the next SV.
    self.assertFalse(injector.done)
    injector.feed(acknowledgement(second, accepted=False))
    self.assertTrue(injector.done)
    self.assertEqual(injector.error, "receiver rejected assistance")
    injector = MGAInjector([ASSIST], 0.0)
    injector.poll(0.0)
    injector.feed(acknowledgement())
    self.assertTrue(injector.done)
    self.assertIsNone(injector.error)


class TestUARTAssistance(unittest.TestCase):
  def test_fragmented_zero_payload_is_identified_as_continuation(self):
    uart = UARTAssistance(Session())
    uart.update(NAVIGATION[:6], 0.0)
    self.assertTrue(uart.partial_frame)
    self.assertEqual(NAVIGATION[6], 0)
    uart.update(NAVIGATION[6:], 0.1)
    self.assertFalse(uart.partial_frame)

  def test_probe_and_injection_do_not_read_or_consume_uart_data(self):
    session = Session()
    uart = UARTAssistance(session)
    session.request_identity()
    polls, status = uart.update(b"", 0.0)
    self.assertEqual(len(polls), 2)
    self.assertEqual(status, [])
    data = NAVIGATION + bytes.fromhex(IDENTITY["UBX-MON-VER"]) + bytes.fromhex(IDENTITY["UBX-SEC-UNIQID"])
    preserved = bytes(data)
    for offset in range(0, len(data), 13):
      uart.update(data[offset:offset + 13], 0.2)
    self.assertEqual(data, preserved)
    self.assertTrue(session.identity_ready.is_set())
    self.assertEqual(session.identity, IDENTITY)
    self.assertFalse(uart.busy)
    session.set_messages([ASSIST])
    self.assertEqual(uart.update(NAVIGATION, 0.3), ([ASSIST], []))
    self.assertTrue(uart.busy)
    self.assertEqual(uart.update(NAVIGATION + acknowledgement(), 0.4), ([], ["AssistNow messages acknowledged by receiver"]))
    self.assertFalse(uart.busy)

  def test_missing_identity_is_bounded_and_retryable(self):
    session = Session()
    uart = UARTAssistance(session)
    session.request_identity()
    uart.update(b"", 0.0)
    uart.update(b"", 1.0)
    self.assertTrue(session.identity_ready.is_set())
    self.assertIsNone(session.identity)
    self.assertFalse(uart.busy)
    session.request_identity()
    self.assertEqual(len(uart.update(b"", 60.0)[0]), 2)

  def test_stale_batch_is_discarded_before_any_frame_is_sent(self):
    session = Session()
    uart = UARTAssistance(session)
    session.set_messages([TIME_UTC, ASSIST], received_at=10.0)
    self.assertEqual(uart.update(NAVIGATION, 15.1), ([], ["discarded stale AssistNow messages"]))
    self.assertFalse(uart.busy)
    self.assertTrue(session.batches.empty())

  def test_cancelled_session_cannot_inject_or_supply_new_session(self):
    old = Session()
    old_uart = UARTAssistance(old)
    old.set_messages([ASSIST])
    old.cancelled.set()
    self.assertEqual(old_uart.update(b"", 0.0), ([], []))
    old.set_identity(IDENTITY)
    self.assertFalse(old.identity_ready.is_set())
    new = Session()
    new_uart = UARTAssistance(new)
    self.assertEqual(new_uart.update(acknowledgement(), 0.0), ([], []))
    new.request_identity()
    self.assertEqual(len(new_uart.update(b"", 1.0)[0]), 2)


class TestAssistNowProtocol(unittest.TestCase):
  def test_only_exact_successful_capability_allows_identity(self):
    for result, expected in [(response(200, headers={PROTOCOL_HEADER: PROTOCOL_VERSION}), True),
                             (response(404), False), (response(405), False), (response(200), False), (response(204), False)]:
      with self.subTest(status=result.status_code, headers=result.headers):
        api = FakeAPI([result])
        self.assertEqual(AssistNowClient(api, "device").supports_receiver(), expected)
        method, endpoint, args = api.calls[0]
        self.assertEqual((method, endpoint), ("GET", "v1/device/assist/receiver"))
        self.assertEqual(args["timeout"], 5)
        self.assertEqual(args["access_token"], "test-device-jwt")
        self.assertNotIn("json", args)
    for result in (response(503), response(401), response(403), response(302),
                   response(200, headers={PROTOCOL_HEADER: "ubx-v2"}), response(404, headers={PROTOCOL_HEADER: PROTOCOL_VERSION})):
      with self.subTest(status=result.status_code), self.assertRaises(RetryLater):
        AssistNowClient(FakeAPI([result]), "device").supports_receiver()

  def test_handshake_never_follows_redirects(self):
    api = FakeAPI([response(200, headers={PROTOCOL_HEADER: PROTOCOL_VERSION}), response(202)])
    client = AssistNowClient(api, "device")
    client.supports_receiver()
    client.register_receiver(IDENTITY)
    mocked_requests = SimpleNamespace(request=Mock(return_value=response(307)))
    with patch.dict(sys.modules, {"requests": mocked_requests}):
      for _, _, args in api.calls:
        args["session"].request("POST", "https://backend.test/v1/device/assist/receiver", json={"messages": IDENTITY})
        self.assertFalse(mocked_requests.request.call_args.kwargs["allow_redirects"])

  def test_receiver_registration_payload_and_http_contract(self):
    for code in (200, 202):
      api = FakeAPI([response(code)])
      AssistNowClient(api, "device").register_receiver(IDENTITY)
      self.assertEqual(api.calls[0][:2], ("POST", "v1/device/assist/receiver"))
      self.assertEqual(api.calls[0][2]["json"], {"messages": IDENTITY})
    for code in (204, 301, 403, 422, 429, 503):
      with self.subTest(code=code), self.assertRaises(RetryLater) as error:
        AssistNowClient(FakeAPI([response(code, headers={"Retry-After": "37"})]), "device").register_receiver(IDENTITY)
      self.assertEqual(error.exception.delay, 37.0)

  def test_only_200_binary_assistance_is_accepted(self):
    api = FakeAPI([response(200, ASSIST)])
    self.assertEqual(AssistNowClient(api, "device").fetch(), [ASSIST])
    self.assertEqual(api.calls[0][:2], ("GET", "v1/device/assist"))
    self.assertNotIn("session", api.calls[0][2])  # Existing download behavior stays compatible.
    with self.assertRaises(ValueError):
      AssistNowClient(FakeAPI([response(200, b'{"state":"ready"}')]), "device").fetch()
    for code in (202, 204, 403, 422, 429, 503):
      with self.subTest(code=code), self.assertRaises(RetryLater):
        AssistNowClient(FakeAPI([response(code, ASSIST)]), "device").fetch()

  def test_retry_after_is_bounded(self):
    for value, expected in [("37", 37.0), ("0", 1.0), ("99999", 99999.0), ("9999999", 2592000.0),
                            ("nan", 10.0), ("inf", 10.0), ("garbage", 10.0),
                            ("Sun, 06 Sep 2020 12:00:00 GMT", 1.0)]:
      self.assertEqual(retry_delay(response(headers={"Retry-After": value})), expected)

  def test_machine_error_extraction_is_bounded_and_does_not_echo_details(self):
    result = response(428, b'{"error":"receiver_required","message":"credential secret-value"}', {"Retry-After": "3600"})
    error = response_error(result, "assistance unavailable")
    self.assertEqual(error.code, "receiver_required")
    self.assertEqual(error.delay, 3600.0)
    self.assertEqual(str(error), "assistance unavailable")
    for body in (b'{"error":"bad code"}', b'{"error":123}', b'["receiver_required"]', b"\xff", b"x" * 4097):
      self.assertIsNone(response_error(response(400, body), "invalid").code)

  def test_direct_token_never_probes_backend(self):
    session = Session()
    factory = Mock(side_effect=AssertionError("proxy accessed"))
    download(session, factory, lambda: [ASSIST], lambda: True, Mock())
    self.assertEqual(session.batches.get_nowait().messages, [ASSIST])
    factory.assert_not_called()
    self.assertFalse(session.probe_requested.is_set())

  def test_legacy_backend_never_receives_identity(self):
    session = Session()
    api = FakeAPI([response(404), response(200, ASSIST)])
    download(session, lambda: AssistNowClient(api, "device"), None, lambda: True, Mock())
    self.assertEqual([call[0] for call in api.calls], ["GET", "GET"])
    self.assertFalse(session.probe_requested.is_set())
    self.assertEqual(session.batches.get_nowait().messages, [ASSIST])

  def test_supported_backend_registers_before_fetch_and_does_not_repost(self):
    session = Session()
    api = FakeAPI([response(200, headers={PROTOCOL_HEADER: PROTOCOL_VERSION}), response(202),
                   response(202, headers={"Retry-After": "15"}), response(200, ASSIST)])
    delays = []

    def identity_reply(_timeout):
      self.assertTrue(session.probe_requested.is_set())
      session.set_identity(IDENTITY)
      return True

    with patch.object(session.identity_ready, "wait", side_effect=identity_reply), \
         patch.object(session.cancelled, "wait", side_effect=lambda delay: delays.append(delay)):
      download(session, lambda: AssistNowClient(api, "device"), None, lambda: True, Mock())
    self.assertEqual([call[0] for call in api.calls], ["GET", "POST", "GET", "GET"])
    self.assertEqual(delays, [15.0])
    self.assertEqual(session.batches.get_nowait().messages, [ASSIST])

  def test_probe_failure_does_not_fetch_using_old_mapping(self):
    session = Session()
    api = FakeAPI([response(200, headers={PROTOCOL_HEADER: PROTOCOL_VERSION})])

    def failed_probe(_timeout):
      session.set_identity(None)
      return True

    with patch.object(session.identity_ready, "wait", side_effect=failed_probe), \
         patch.object(session.cancelled, "wait", side_effect=lambda _delay: session.cancelled.set()):
      download(session, lambda: AssistNowClient(api, "device"), None, lambda: True, Mock())
    self.assertEqual(len(api.calls), 1)
    self.assertTrue(session.batches.empty())

  def test_late_http_reply_is_fenced_after_receiver_reset(self):
    session = Session()
    entered = threading.Event()
    release = threading.Event()

    def delayed_fetch():
      entered.set()
      release.wait(2.0)
      return [ASSIST]

    worker = threading.Thread(target=download, args=(session, Mock(), delayed_fetch, lambda: True, Mock()))
    worker.start()
    try:
      self.assertTrue(entered.wait(1.0))
      session.cancelled.set()
    finally:
      release.set()
      worker.join(2.0)
    self.assertFalse(worker.is_alive())
    self.assertTrue(session.batches.empty())

  def test_cancellation_after_capability_prevents_registration(self):
    session = Session()
    client = Mock()

    def capabilities():
      session.cancelled.set()
      return True

    client.supports_receiver.side_effect = capabilities
    download(session, lambda: client, None, lambda: True, Mock())
    client.register_receiver.assert_not_called()
    client.fetch.assert_not_called()

  def test_transient_capability_failure_cannot_trigger_legacy_fetch(self):
    session = Session()
    api = FakeAPI([response(503, headers={"Retry-After": "9"}), response(404), response(200, ASSIST)])
    delays = []
    with patch.object(session.cancelled, "wait", side_effect=lambda delay: delays.append(delay)):
      download(session, lambda: AssistNowClient(api, "device"), None, lambda: True, Mock())
    self.assertEqual([call[1] for call in api.calls], ["v1/device/assist/receiver", "v1/device/assist/receiver", "v1/device/assist"])
    self.assertEqual(delays, [9.0])

  def test_lost_receiver_mapping_requires_new_probe_and_post(self):
    for code in ("receiver_required", "association_changed"):
      with self.subTest(code=code):
        session = Session()
        api = FakeAPI([response(200, headers={PROTOCOL_HEADER: PROTOCOL_VERSION}), response(200),
                       response(428, ('{"error":"' + code + '"}').encode()), response(200), response(200, TIME_UTC + ASSIST)])

        def identity_reply(_timeout, session=session):
          session.set_identity(IDENTITY)
          return True

        with patch.object(session.identity_ready, "wait", side_effect=identity_reply) as probe_wait, \
             patch.object(session.cancelled, "wait", return_value=False):
          download(session, lambda api=api: AssistNowClient(api, "device"), None, lambda: True, Mock())
        self.assertEqual(probe_wait.call_count, 2)
        self.assertEqual([call[0] for call in api.calls], ["GET", "POST", "GET", "POST", "GET"])
        self.assertEqual(session.batches.get_nowait().messages, [TIME_UTC, ASSIST])

  def test_long_provider_cooldown_wait_is_interrupted_by_receiver_reset(self):
    session = Session()
    api = FakeAPI([response(404), response(429, b'{"error":"quota"}', {"Retry-After": "86400"})])
    waiting = threading.Event()
    original_wait = session.cancelled.wait

    def wait(delay):
      self.assertEqual(delay, 86400)
      waiting.set()
      return original_wait(delay)

    with patch.object(session.cancelled, "wait", side_effect=wait):
      worker = threading.Thread(target=download, args=(session, lambda: AssistNowClient(api, "device"), None, lambda: True, Mock()))
      worker.start()
      try:
        self.assertTrue(waiting.wait(1.0))
      finally:
        session.cancelled.set()
        worker.join(1.0)
      self.assertFalse(worker.is_alive())


def load_pigeond_without_hardware():
  """Load the production module, replacing only external/device dependencies."""
  modules = {
    "requests": SimpleNamespace(get=Mock(side_effect=AssertionError("unexpected live HTTP request"))),
    "openpilot.cereal": SimpleNamespace(log=Mock(), messaging=Mock()),
    "openpilot.common.api": SimpleNamespace(Api=Mock()),
    "openpilot.common.time_helpers": SimpleNamespace(system_time_valid=lambda: True),
    "openpilot.common.params": SimpleNamespace(Params=Mock()),
    "openpilot.common.serial": SimpleNamespace(Serial=Mock(side_effect=AssertionError("unexpected real UART open"))),
    "openpilot.common.swaglog": SimpleNamespace(cloudlog=Mock()),
    "openpilot.common.hardware": SimpleNamespace(COMMA_HARDWARE=True),
    "openpilot.common.gpio": SimpleNamespace(gpio_init=Mock(), gpio_set=Mock()),
    "openpilot.common.hardware.comma.pins": SimpleNamespace(GPIO=Mock()),
  }
  spec = importlib.util.spec_from_file_location("pigeond_host_test", Path(__file__).parents[1] / "pigeond.py")
  module = importlib.util.module_from_spec(spec)
  with patch.dict(sys.modules, modules):
    spec.loader.exec_module(module)
  return module


class TestPigeondIntegration(unittest.TestCase):
  def run_main_loop(self, *, reset=False, stale=False, write_failure=False):
    module = load_pigeond_without_hardware()
    clock = SimpleNamespace(now=0.0)
    module.time = SimpleNamespace(monotonic=lambda: clock.now, sleep=lambda duration: setattr(clock, "now", clock.now + duration))
    module.threading = SimpleNamespace(Thread=Mock())  # The HTTP worker is tested independently above.
    published = []
    received = []
    sent = []
    chunks = deque([b"\x00", NAVIGATION] if reset else [NAVIGATION])
    sessions = []
    next_message = ubx_message(b"\x13\x00", b"\x01\x00\x07\x00" + bytes(64))

    class TestSession(Session):
      def __init__(self):
        super().__init__()
        message = next_message if sessions else ASSIST
        self.set_messages([TIME_UTC, message], received_at=-10.0 if stale else clock.now)
        sessions.append(self)

    def receive():
      clock.now += 0.1
      data = chunks.popleft() if chunks else NAVIGATION
      received.append(data)
      return data

    def send(message):
      sent.append(message)
      if write_failure:
        raise OSError("simulated UART failure")
      ack = acknowledgement(message)
      # Force a valid continuation to begin with the zero-valued version byte.
      chunks.extend([ack[:7], ack[7:] + NAVIGATION])

    module.Session = TestSession
    module.TTYPigeon = Mock(return_value=SimpleNamespace(receive=receive, send=send))
    module.init = Mock()
    module.messaging = SimpleNamespace(
      PubMaster=lambda _: SimpleNamespace(send=lambda _name, message: published.append(message.ubloxRaw)),
      new_message=lambda *_args, **_kwargs: SimpleNamespace(ubloxRaw=None),
    )
    module.run_receiving(duration=1)
    return module, received, published, sent, sessions

  def test_main_loop_forwards_navigation_during_fragmented_time_and_mga_acks(self):
    module, received, published, sent, sessions = self.run_main_loop()
    self.assertEqual(sent, [TIME_UTC, ASSIST])
    self.assertEqual(published, received)
    self.assertEqual(module.init.call_count, 1)
    self.assertTrue(sessions[0].cancelled.is_set())
    module.TTYPigeon.assert_called_once_with()

  def test_main_loop_reset_discards_old_batch_and_initializes_new_session(self):
    module, received, published, sent, sessions = self.run_main_loop(reset=True)
    self.assertEqual(len(sessions), 2)
    self.assertTrue(all(session.cancelled.is_set() for session in sessions))
    self.assertEqual(published, received[1:])
    self.assertEqual(module.init.call_count, 2)
    self.assertNotIn(ASSIST, sent)
    self.assertEqual(len(sent), 2)
    self.assertEqual(sent[0], TIME_UTC)

  def test_main_loop_stale_download_does_not_stop_navigation(self):
    _, received, published, sent, _ = self.run_main_loop(stale=True)
    self.assertEqual(sent, [])
    self.assertEqual(published, received)

  def test_main_loop_assistance_write_failure_does_not_stop_navigation(self):
    module, received, published, sent, _ = self.run_main_loop(write_failure=True)
    self.assertEqual(sent, [TIME_UTC])
    self.assertEqual(published, received)
    self.assertEqual(module.init.call_count, 1)

  def test_initial_time_rejection_does_not_restart_receiver_configuration(self):
    module = load_pigeond_without_hardware()
    pigeon = Mock()
    pigeon.wait_for_backup_restore_status.return_value = 3

    def send_with_ack(_message, **kwargs):
      if kwargs.get("ack") == module.UBLOX_ASSIST_ACK:
        raise TimeoutError("time rejected")

    pigeon.send_with_ack.side_effect = send_with_ack
    self.assertTrue(module.init_pigeon(pigeon))
    pigeon.wait_for_backup_restore_status.assert_called_once_with()

  def test_initial_time_ack_timeout_does_not_resend_old_utc(self):
    module = load_pigeond_without_hardware()
    clock = SimpleNamespace(now=0.0)
    module.time = SimpleNamespace(monotonic=lambda: clock.now, sleep=lambda duration: setattr(clock, "now", clock.now + duration))
    pigeon = object.__new__(module.TTYPigeon)
    pigeon.send = Mock()
    pigeon.receive = lambda: b""
    with self.assertRaises(TimeoutError):
      pigeon.send_with_ack(TIME_UTC, ack=module.UBLOX_ASSIST_ACK)
    pigeon.send.assert_called_once_with(TIME_UTC)


if __name__ == "__main__":
  unittest.main()
