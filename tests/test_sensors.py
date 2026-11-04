import unittest

from custom_components.piston.sensor import SCREENS, DeckySensor, SteamScreenSensor, async_setup_entry
from homeassistant.config_entries import ConfigEntry
from .entityutil import connect, make_coordinator


class Collector:
    def __init__(self):
        self.entities = []

    def __call__(self, entities):
        self.entities.extend(entities)


class SensorTests(unittest.IsolatedAsyncioTestCase):
    async def setup(self, **groups):
        coord = make_coordinator()
        connect(coord, **groups)
        entry = ConfigEntry()
        entry.runtime_data = coord
        entry.async_on_unload = lambda f: setattr(entry, "stop", f)
        add = Collector()
        await async_setup_entry(coord.hass, entry, add)
        return coord, add

    async def test_creates_an_entity_per_reported_key_with_table_attributes(self):
        coord, add = await self.setup(sensors={"cpu_temp": 52.5, "uptime": 99, "ip": "10.0.0.2", "weird_key": 1})
        by_key = {e.unique_id: e for e in add.entities if isinstance(e, DeckySensor)}
        self.assertEqual(sorted(by_key), ["dev123abc456_sensor_cpu_temp", "dev123abc456_sensor_ip", "dev123abc456_sensor_uptime"])
        temp = by_key["dev123abc456_sensor_cpu_temp"]
        self.assertEqual((temp.native_value, temp._attr_device_class, temp._attr_native_unit_of_measurement,
                          temp._attr_state_class, temp._attr_name), (52.5, "temperature", "°C", "measurement", "CPU temperature"))
        self.assertEqual(by_key["dev123abc456_sensor_uptime"]._attr_device_class, "duration")

    async def test_unreported_keys_create_nothing_and_new_keys_are_added_later(self):
        coord, add = await self.setup(sensors={"cpu_load": 3})
        self.assertEqual(len([e for e in add.entities if isinstance(e, DeckySensor)]), 1)
        coord.on_state("sensors", {"cpu_load": 4, "battery_main": 80, "gpu_load": 9})
        keys = sorted(e._key for e in add.entities if isinstance(e, DeckySensor))
        self.assertEqual(keys, ["battery_main", "cpu_load", "gpu_load"])
        coord.on_state("sensors", {"cpu_load": 5, "battery_main": 79, "gpu_load": 9})
        self.assertEqual(len([e for e in add.entities if isinstance(e, DeckySensor)]), 3)  # no duplicates
        battery = next(e for e in add.entities if getattr(e, "_key", "") == "battery_main")
        self.assertEqual((battery._attr_name, battery._attr_device_class), ("Battery main", "battery"))

    async def test_values_and_availability_follow_the_state(self):
        coord, add = await self.setup(sensors={"cpu_load": 3})
        s = next(e for e in add.entities if isinstance(e, DeckySensor))
        self.assertEqual((s.native_value, s.available), (3, True))
        coord.on_state("sensors", {})  # the device stopped reporting this field
        self.assertFalse(s.available)
        coord.on_state("sensors", {"cpu_load": 7})
        coord.on_connection(False)
        self.assertFalse(s.available)

    async def test_steam_screen(self):
        coord, add = await self.setup(apps={"screen": "steam_over_game", "running": True})
        screen = next(e for e in add.entities if isinstance(e, SteamScreenSensor))
        self.assertEqual((screen.native_value, screen.available), ("steam_over_game", True))
        self.assertEqual(screen._attr_options, SCREENS)
        coord.on_state("apps", {"screen": "steam"})
        self.assertEqual(screen.native_value, "steam")

    async def test_steam_screen_unavailable_before_the_first_report_and_offline(self):
        coord, add = await self.setup()
        screen = next(e for e in add.entities if isinstance(e, SteamScreenSensor))
        self.assertFalse(screen.available)
        coord.on_state("apps", {"screen": "game"})
        self.assertTrue(screen.available)
        coord.on_connection(False)
        self.assertFalse(screen.available)
