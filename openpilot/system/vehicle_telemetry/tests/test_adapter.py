import copy
import unittest


class TestMQBAdapter(unittest.TestCase):
  def setUp(self):
    from opendbc.can import CANPacker
    from opendbc.car.structs import car
    from openpilot.system.vehicle_telemetry.adapters import adapter_for
    self.cp = car.CarParams.new_message(brand="volkswagen", carFingerprint="AUDI_A3_MK3", notCar=False,
                                       networkLocation="gateway", safetyConfigs=[{}])
    self.packer = CANPacker("vw_mqb")
    self.adapter = adapter_for(self.cp)

  def update(self, message, values, mono=100, bus=0):
    frame = self.packer.make_can_msg(message, bus, values)
    return self.adapter.update([(int(mono * 1e9), [frame])], mono, 1000 + mono)

  def test_missing_frames_never_produce_defaults_and_zero_is_valid(self):
    self.assertEqual(self.adapter.update([], 100, 1100), {})
    result = self.update("Kombi_02", {"KBI_Kilometerstand": 0, "KBI_Inhalt_Tank": 0})
    self.assertEqual(result["odometer_km"][0], 0)
    self.assertEqual(result["fuel_liters"][0], 0)
    self.assertNotIn("fuel_percent", result)

  def test_real_mqb_packed_signals_and_high_resolution_precedence(self):
    self.update("Kombi_02", {"KBI_Kilometerstand": 45000, "KBI_Inhalt_Tank": 37})
    result = self.update("Kombi_03", {"KBI_Tankinhalt_hochaufl": 36.42, "KBI_Tankfuellstand_Prozent": 65})
    self.assertEqual(result["odometer_km"][0], 45000)
    self.assertAlmostEqual(result["fuel_liters"][0], 36.42)
    self.assertEqual(result["fuel_percent"][0], 65)
    self.assertTrue(result["fuel_liters"][2].endswith("KBI_Tankinhalt_hochaufl"))

  def test_error_sentinels_fallback_and_stale_samples(self):
    self.update("Kombi_02", {"KBI_Kilometerstand": 1048575, "KBI_Inhalt_Tank": 37})
    result = self.update("Kombi_03", {"KBI_Tankinhalt_hochaufl": 163.83, "KBI_Tankfuellstand_Prozent": 127})
    self.assertEqual(result["fuel_liters"][0], 37)
    self.assertNotIn("fuel_percent", result)
    self.assertNotIn("odometer_km", result)
    self.assertEqual(self.adapter.update([], 111, 1111), {})

  def test_init_sentinels_and_range_validation(self):
    result = self.update("Kombi_02", {"KBI_Kilometerstand": 1048574, "KBI_Inhalt_Tank": 126})
    self.assertEqual(result, {})
    result = self.update("Kombi_03", {"KBI_Tankinhalt_hochaufl": 163.82, "KBI_Tankfuellstand_Prozent": 126})
    self.assertEqual(result, {})

  def test_tank_fault_rejects_coarse_liters_without_discarding_odometer(self):
    result = self.update("Kombi_02", {"KBI_Kilometerstand": 50000, "KBI_Inhalt_Tank": 30, "KBI_FStatus_Tank": 1})
    self.assertNotIn("fuel_liters", result)
    self.assertEqual(result["odometer_km"][0], 50000)

  def test_bus_offsets_and_no_camera_bus_fallback(self):
    from openpilot.system.vehicle_telemetry.adapters import adapter_for
    self.cp.safetyConfigs = [{}, {}]
    self.adapter = adapter_for(self.cp)
    self.assertEqual(self.adapter.parser.bus, 4)
    self.assertEqual(self.update("Kombi_02", {"KBI_Kilometerstand": 42}, bus=0), {})
    self.assertEqual(self.update("Kombi_02", {"KBI_Kilometerstand": 42}, bus=6), {})
    self.assertEqual(self.update("Kombi_02", {"KBI_Kilometerstand": 42}, bus=4)["odometer_km"][0], 42)

  def test_truncated_or_queued_old_packets_cannot_manufacture_zero(self):
    self.assertEqual(self.adapter.update([(100_000_000_000, [(0x6B7, b"\x00", 0)])], 100, 1100), {})
    frame = self.packer.make_can_msg("Kombi_02", 0, {"KBI_Kilometerstand": 42})
    self.assertEqual(self.adapter.update([(80_000_000_000, [frame])], 100, 1100), {})

  def test_unknown_other_platform_and_not_car_are_unsupported(self):
    from openpilot.system.vehicle_telemetry.adapters import adapter_for
    for fingerprint in ("UNKNOWN", "VOLKSWAGEN_PASSAT_NMS", "VOLKSWAGEN_ID4_MK1"):
      self.cp.carFingerprint = fingerprint
      self.assertIsNone(adapter_for(self.cp))
    self.cp.carFingerprint = "AUDI_A3_MK3"
    self.cp.notCar = True
    self.assertIsNone(adapter_for(self.cp))

  def test_optional_parser_does_not_change_driving_parser(self):
    from opendbc.can import CANParser
    driving_parser = CANParser("vw_mqb", [], 0)
    before = copy.deepcopy(driving_parser.addresses)
    self.update("Kombi_02", {"KBI_Kilometerstand": 42})
    self.assertEqual(driving_parser.addresses, before)
    self.assertNotEqual(self.adapter.parser.addresses, before)
