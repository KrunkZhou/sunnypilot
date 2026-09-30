"""Manager-owned hotspot-local relay receiver. Never opens a wildcard socket."""
from __future__ import annotations

import signal
import socket
import time

from openpilot.sunnypilot.external_navigation.config import load_config
from openpilot.sunnypilot.external_navigation.settings import read_mode
from openpilot.sunnypilot.external_navigation.policy import RIGHT_BRANCH_TYPES
from openpilot.sunnypilot.external_navigation.protocol import PORT, MAX_PACKET, UNKNOWN_AGE, ProtocolError, Receiver


def publish(pm, receiver: Receiver | None, now: int, status: str, rejection: str = '') -> None:
  from openpilot.cereal import messaging
  message = messaging.new_message('externalNavigationSP')
  message.valid = True  # Receiver health is independent of optional guidance availability.
  out = message.externalNavigationSP
  out.status = status
  out.rejectionReason = rejection
  out.routeState = 255
  out.distanceAgeMs = out.maneuverAgeMs = out.sourceAgeMs = UNKNOWN_AGE
  if receiver is not None:
    out.relayId = receiver.relay
    out.receiverSession = receiver.instance
    out.connected = 0 <= now - receiver.received <= 1000
    out.receiveMonoTime = max(0, receiver.received) * 1000000
    out.transportRttMs = receiver.rtt_ms
    if (nav := receiver.navigation) is not None and out.connected:
      f = nav.fields
      out.available = nav.available
      out.publisherSession = nav.session
      out.cacheEpoch, out.streamId, out.generation = nav.epoch, nav.stream, nav.generation
      out.token, out.snapshotSequence = nav.token, nav.sequence
      out.hasManeuverId = 16 in f
      out.maneuverId = f.get(16, 0)
      out.distanceObservation, out.maneuverObservation = f.get(17, 0), f.get(19, 0)
      out.distanceAgeMs, out.maneuverAgeMs = f.get(18, UNKNOWN_AGE), f.get(20, UNKNOWN_AGE)
      out.sourceAgeMs = nav.source_age
      out.routeState, out.maneuverType = f[1], f.get(2, 0)
      out.hasManeuver = 2 in f
      out.hasNextUnits, out.nextUnits = 7 in f, f.get(7, 0)
      out.hasTripUnits, out.tripUnits = 9 in f, f.get(9, 0)
      out.hasNextDistance, out.nextDistance = 3 in f, f.get(3, 0)
      out.sourceFresh = nav.supports_hints and out.distanceAgeMs + max(0, now - receiver.received) <= 1000
      out.instruction, out.road = f.get(12, ''), f.get(11, '') or f.get(10, '')
      out.currentRoad, out.destination, out.laneDescription = f.get(10, ''), f.get(13, ''), f.get(14, '')
      out.nextDisplay, out.tripDisplay = f.get(6, ''), f.get(8, '')
      out.branchRight = f.get(2) in RIGHT_BRANCH_TYPES
      out.hasTripDistance, out.tripDistance = 4 in f, f.get(4, 0)
      out.hasTripSeconds, out.tripSeconds = 5 in f, f.get(5, 0)
      out.lanes = f.get(15, [])
  pm.send('externalNavigationSP', message)


def hotspot_address() -> str | None:
  """Match the actual wlan0 AP address, never LAN/default-route/all interfaces."""
  import fcntl
  import struct
  try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as query:
      result = fcntl.ioctl(query.fileno(), 0x8915, struct.pack('256s', b'wlan0'))
    address = socket.inet_ntoa(result[20:24])
    from openpilot.system.ui.lib.wifi_manager import TETHERING_IP_ADDRESS
    return address if address == TETHERING_IP_ADDRESS else None
  except (OSError, ImportError):
    return None


def main() -> None:
  from openpilot.cereal import messaging
  from openpilot.common.params import Params
  from openpilot.sunnypilot.external_navigation.hotspot import HotspotWorker
  from openpilot.sunnypilot.external_navigation.diagnostics import DiagnosticsWriter, receiver_record

  params = Params()
  pm = messaging.PubMaster(['externalNavigationSP'])
  sm = messaging.SubMaster(['deviceState'])
  hotspot = HotspotWorker(params)
  diagnostics = DiagnosticsWriter()
  running = True
  def stop(signum, frame):
    nonlocal running
    running = False
  signal.signal(signal.SIGTERM, stop)
  signal.signal(signal.SIGINT, stop)
  receiver, sock = None, None
  next_config_read = next_address_read = next_publish = next_mode_read = 0.
  enabled = False
  address = None
  rejection, status = '', 'off'
  try:
    while running:
      now_s = time.monotonic()
      now = int(now_s * 1000)
      sm.update(0)
      if now_s >= next_mode_read:
        mode = read_mode()
        enabled = mode == 1
        hotspot.update(bool(sm['deviceState'].started), enabled)
        next_mode_read = now_s + .2
      if now_s >= next_config_read:
        next_config_read = now_s + 5
        try:
          identity, key = load_config()
          if receiver is None or (receiver.relay, receiver.key) != (identity, key):
            receiver = Receiver(identity, key)
        except (OSError, ValueError, KeyError, TypeError):
          receiver = None
      if now_s >= next_address_read:
        address = hotspot_address()
        next_address_read = now_s + 1
      want_socket = enabled and receiver is not None and address is not None and hotspot.is_hotspot_active()
      if not want_socket:
        if sock is not None:
          sock.close()
          sock = None
        if receiver is not None:
          receiver.clear('off' if not enabled else 'hotspot_unavailable')
        status = 'off' if not enabled else ('not_provisioned' if receiver is None else 'hotspot_unavailable')
      else:
        if sock is None:
          try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * MAX_PACKET)
            sock.bind((address, PORT))
            sock.setblocking(False)
          except OSError:
            if sock is not None:
              sock.close()
            sock = None
            status = 'socket_unavailable'
        if sock is not None:
          # A bounded batch prevents an unauthenticated flood starving lease expiry/publication.
          for _ in range(8):
            try:
              packet, peer = sock.recvfrom(MAX_PACKET + 1)
            except BlockingIOError:
              break
            except OSError:
              sock.close()
              sock = None
              receiver.clear('socket_lost')
              break
            try:
              receiver.receive(packet, peer, now)
              rejection = ''
            except ProtocolError as exc:
              rejection = str(exc)  # Only fixed reason codes, never packet/secret contents.
          receiver.tick(now)
          if sock is not None and (challenge := receiver.challenge(now)):
            try:
              sock.sendto(challenge, receiver.peer)
            except OSError:
              sock.close()
              sock = None
              receiver.clear('send_failed')
          status = receiver.reason
      if now_s >= next_publish:
        publish(pm, receiver if enabled else None, now, status, rejection)
        if enabled:
          diagnostics.submit(receiver_record(receiver, now, status, rejection))
        next_publish = now_s + .2
      time.sleep(.01)
  finally:
    if sock is not None:
      sock.close()
    hotspot.close()
    diagnostics.close()
    publish(pm, None, int(time.monotonic() * 1000), 'stopped')


if __name__ == '__main__':
  main()
