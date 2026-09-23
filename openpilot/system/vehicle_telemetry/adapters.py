"""Optional passive vehicle readings. These parsers never participate in controls."""
import math

from opendbc.can import CANParser
from opendbc.car import Bus
from opendbc.car.volkswagen.values import CanBus, DBC

FRAME_MAX_AGE = 10.0


class VolkswagenMQBAdapter:
  def __init__(self, CP):
    self.parser = CANParser("vw_mqb", [("Kombi_02", 0), ("Kombi_03", 0)], CanBus(CP).pt)
    self.candidates = {}
    self.last_frame = None

  def update(self, packets, monotonic_now: float, wall_now: float) -> dict:
    # CANParser also accepts short packets; reject them explicitly so truncated
    # frames cannot manufacture plausible zero readings.
    packets = [(nanos, [(addr, data, src) for addr, data, src in frames
                        if addr in self.parser.addresses and len(data) == self.parser.message_states[addr].size])
               for nanos, frames in packets if 0 <= monotonic_now - nanos / 1e9 <= FRAME_MAX_AGE]
    updated = self.parser.update(packets)
    for message, signal, maximum in (
      ("Kombi_02", "KBI_Kilometerstand", 1048573),
      ("Kombi_02", "KBI_Inhalt_Tank", 125),
      ("Kombi_03", "KBI_Tankinhalt_hochaufl", 163.81),
      ("Kombi_03", "KBI_Tankfuellstand_Prozent", 100),
    ):
      address = self.parser.dbc.name_to_msg[message].address
      if address not in updated:
        continue
      value = self.parser.vl[message][signal]
      source = f"vw_mqb:{message}.{signal}"
      tank_fault = signal == "KBI_Inhalt_Tank" and self.parser.vl[message]["KBI_FStatus_Tank"] != 0
      if not tank_fault and math.isfinite(value) and 0 <= value <= maximum:
        received = self.parser.ts_nanos[message][signal] / 1e9
        self.candidates[signal] = (value, wall_now - (monotonic_now - received), source, received)
        self.last_frame = received if self.last_frame is None else max(self.last_frame, received)
      else:
        self.candidates.pop(signal, None)

    result = {}
    for metric, signals in (
      ("odometer_km", ("KBI_Kilometerstand",)),
      ("fuel_percent", ("KBI_Tankfuellstand_Prozent",)),
      ("fuel_liters", ("KBI_Tankinhalt_hochaufl", "KBI_Inhalt_Tank")),
    ):
      for signal in signals:
        sample = self.candidates.get(signal)
        if sample is not None and 0 <= monotonic_now - sample[3] <= FRAME_MAX_AGE:
          result[metric] = sample[:3]
          break
    return result


def adapter_for(CP):
  # A DBC match limits this to MQB (not PQ, MLB, MEB or MQB Evo), and requires
  # a real identified car. In particular this includes AUDI_A3_MK3 / 8V.
  if not CP.notCar and CP.brand == "volkswagen" and DBC.get(CP.carFingerprint, {}).get(Bus.pt) == "vw_mqb":
    return VolkswagenMQBAdapter(CP)
  return None
