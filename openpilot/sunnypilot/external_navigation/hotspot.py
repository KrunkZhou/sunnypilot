"""Own only the hotspot transition initiated by external navigation.

The receiver never waits on D-Bus. NetworkManager work runs on a separate,
bounded worker; a manual network action revokes ownership for this road session.
"""
import threading
import time
from dataclasses import dataclass
from openpilot.sunnypilot.external_navigation.settings import read_network_revision


@dataclass
class NetworkState:
  ready: bool
  connected: str | None
  connecting: str | None
  hotspot: str
  manual_revision: str


class HotspotPolicy:
  def __init__(self):
    self.wanted = False
    self.owned = False
    self.attempted = False
    self.revoked = False
    self.previous = None
    self.manual_revision = ""
    self.started_at = 0.0
    self.was_connected = False

  def update(self, wanted: bool, network: NetworkState, now: float):
    """Return at most one (activate/deactivate, SSID) action per session."""
    if not wanted:
      action = None
      if (self.owned and network.manual_revision == self.manual_revision and
          network.hotspot in (network.connected, network.connecting)):
        action = ("activate", self.previous) if self.previous else ("deactivate", network.hotspot)
      self.__init__()
      return action
    if not self.wanted:
      self.wanted = True
      self.manual_revision = network.manual_revision
    if self.manual_revision != network.manual_revision:
      self.revoked = True
      self.owned = False
    if self.revoked or not network.ready:
      return None
    if not self.attempted:
      # Do not interfere with an in-progress manual connection or an existing AP.
      if network.connecting:
        return None
      self.attempted = True
      if network.connected == network.hotspot:
        return None
      self.previous = network.connected
      self.owned = True
      self.started_at = now
      return ("activate", network.hotspot)
    if self.owned:
      if network.connected == network.hotspot:
        self.was_connected = True
      elif self.was_connected or (network.connecting and network.connecting != network.hotspot) or now - self.started_at > 15:
        # An external nmcli/user change or a failed start is not permission to
        # continually steal the radio back. Explicit off/on starts another try.
        self.owned = False
        self.revoked = True
    return None


class HotspotWorker:
  def __init__(self, params):
    self._condition = threading.Condition()
    self._wanted = False
    self._enabled = False
    self._ap_ready = False
    self._closed = False
    self._thread = threading.Thread(target=self._run, name="external-nav-hotspot", daemon=True)
    self._thread.start()

  def update(self, started: bool, enabled: bool):
    with self._condition:
      self._wanted = bool(started and enabled)
      self._enabled = bool(enabled)
      self._condition.notify()

  def is_hotspot_active(self) -> bool:
    return self._ap_ready

  def close(self):
    with self._condition:
      self._wanted = False
      self._closed = True
      self._condition.notify()
    self._thread.join(timeout=1)

  def _run(self):
    manager = None
    policy = HotspotPolicy()
    try:
      while True:
        with self._condition:
          self._condition.wait(timeout=0.5)
          wanted, enabled, closed = self._wanted, self._enabled, self._closed
        if manager is None and enabled and not closed:
          from openpilot.system.ui.lib.wifi_manager import WifiManager
          manager = WifiManager()
          manager.set_active(False)  # No background AP scans competing with BLE.
        if manager is not None:
          revision = read_network_revision()
          network = NetworkState(manager.navigation_network_ready and revision is not None, manager.connected_ssid,
                                 manager.connecting_to_ssid, manager.tethering_ssid,
                                 revision)
          self._ap_ready = network.ready and network.connected == network.hotspot
          action = policy.update(wanted, network, time.monotonic())
          if action:
            if action[0] == "activate":
              manager.activate_connection(action[1], block=True, manual=False, expected_revision=network.manual_revision)
            else:
              manager.set_tethering_active(False, manual=False, block=True, expected_revision=network.manual_revision)
        if closed:
          break
    except Exception:
      # Never include D-Bus replies/settings in this diagnostic (may contain PSK).
      from openpilot.common.swaglog import cloudlog
      cloudlog.warning("External navigation hotspot worker stopped; automatic hotspot unavailable")
    finally:
      self._ap_ready = False
      if manager is not None:
        manager.stop()
