import unittest

from homeassistant.config_entries import ConfigEntry

from custom_components.piston import binary_sensor, sensor
from custom_components.piston.entity import controller_labels
from .entityutil import connect, make_coordinator
from .test_sensors import Collector


def group(connected=None, seen=None):
    return {"connected": connected or {}, "seen": seen or []}


PAD = {"name": "Pro 2", "battery": 52, "charging": False, "wireless": True, "stale": False, "updated": "t"}


class ControllerEntityTests(unittest.IsolatedAsyncioTestCase):
    async def setup(self, **groups):
        coord = make_coordinator()
        connect(coord, **groups)
        entry = ConfigEntry()
        entry.runtime_data = coord
        entry.async_on_unload = lambda f: None
        add_sensor, add_binary = Collector(), Collector()
        await sensor.async_setup_entry(coord.hass, entry, add_sensor)
        await binary_sensor.async_setup_entry(coord.hass, entry, add_binary)
        return coord, add_sensor, add_binary

    def batteries(self, add):
        return [e for e in add.entities if isinstance(e, sensor.ControllerBatterySensor)]

    def chargers(self, add):
        return [e for e in add.entities if isinstance(e, binary_sensor.ControllerChargingSensor)]

    async def test_a_new_controller_adds_a_battery_and_a_charging_entity(self):
        coord, add_s, add_b = await self.setup(controllers=group())
        self.assertEqual((self.batteries(add_s), self.chargers(add_b)), ([], []))
        coord.on_state("controllers", group({"aabb": PAD}, [{"serial": "aabb", "name": "Pro 2"}]))
        b, c = self.batteries(add_s)[0], self.chargers(add_b)[0]
        self.assertEqual((b.unique_id, c.unique_id), ("dev123abc456_controller_aabb_battery", "dev123abc456_controller_aabb_charging"))
        self.assertEqual((b.name if hasattr(b, "name") else b._attr_name, c._attr_name), ("Pro 2 battery", "Pro 2 charging"))
        self.assertEqual((b.native_value, b.available, c.is_on, c.available), (52, True, False, True))
        self.assertEqual(b.extra_state_attributes, {"stale": False, "updated": "t", "wireless": True})

    async def test_no_duplicates_on_later_updates(self):
        coord, add_s, add_b = await self.setup(controllers=group({"aabb": PAD}, [{"serial": "aabb", "name": "Pro 2"}]))
        coord.on_state("controllers", group({"aabb": {**PAD, "battery": 50}}, [{"serial": "aabb", "name": "Pro 2"}]))
        self.assertEqual((len(self.batteries(add_s)), len(self.chargers(add_b))), (1, 1))
        self.assertEqual(self.batteries(add_s)[0].native_value, 50)

    async def test_disconnected_controller_is_unavailable_but_kept(self):
        seen = [{"serial": "aabb", "name": "Pro 2"}]
        coord, add_s, add_b = await self.setup(controllers=group({"aabb": PAD}, seen))
        b, c = self.batteries(add_s)[0], self.chargers(add_b)[0]
        coord.on_state("controllers", group({}, seen))
        self.assertEqual((b.available, c.available, b.native_value, c.is_on), (False, False, None, None))

    async def test_restart_recreates_entities_for_controllers_that_are_not_connected(self):
        coord, add_s, add_b = await self.setup(controllers=group({}, [{"serial": "aabb", "name": "Pro 2"}]))
        self.assertEqual(len(self.batteries(add_s)), 1)
        self.assertFalse(self.batteries(add_s)[0].available)

    async def test_unknown_charging_state_is_none(self):
        coord, add_s, add_b = await self.setup(controllers=group({"aabb": {**PAD, "charging": None, "stale": True}},
                                                                  [{"serial": "aabb", "name": "Pro 2"}]))
        self.assertIsNone(self.chargers(add_b)[0].is_on)
        self.assertTrue(self.batteries(add_s)[0].extra_state_attributes["stale"])

    async def test_same_named_controllers_are_told_apart(self):
        coord = make_coordinator()
        connect(coord, controllers=group({}, [{"serial": "aaaa1111", "name": "Pad"}, {"serial": "bbbb2222", "name": "Pad"},
                                              {"serial": "cccc3333", "name": "Other"}]))
        self.assertEqual(controller_labels(coord), {"aaaa1111": "Pad 1111", "bbbb2222": "Pad 2222", "cccc3333": "Other"})
