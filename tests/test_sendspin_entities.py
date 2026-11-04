import unittest

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from custom_components.piston import sensor, switch
from custom_components.piston.client import CommandError
from .entityutil import connect, make_coordinator
from .test_sensors import Collector

GROUP = {"enabled": True, "status": "waiting", "name": "Box", "port": 8928}


class SendspinEntityTests(unittest.IsolatedAsyncioTestCase):
    async def setup(self, **groups):
        coord = make_coordinator()
        connect(coord, **groups)
        entry = ConfigEntry()
        entry.runtime_data = coord
        entry.unloaders = []
        entry.async_on_unload = entry.unloaders.append
        add_sensor, add_switch = Collector(), Collector()
        await sensor.async_setup_entry(coord.hass, entry, add_sensor)
        await switch.async_setup_entry(coord.hass, entry, add_switch)
        return coord, add_sensor, add_switch

    def only(self, collector, kind):
        found = [e for e in collector.entities if isinstance(e, kind)]
        return found[0] if found else None

    async def test_a_device_without_a_sendspin_player_gets_no_entities(self):
        _, add_sensor, add_switch = await self.setup()
        self.assertIsNone(self.only(add_sensor, sensor.SendspinStatusSensor))
        self.assertIsNone(self.only(add_switch, switch.SendspinSwitch))

    async def test_a_device_that_reports_the_group_gets_a_switch_and_a_diagnostic_sensor(self):
        _, add_sensor, add_switch = await self.setup(sendspin=GROUP)
        st, sw = self.only(add_sensor, sensor.SendspinStatusSensor), self.only(add_switch, switch.SendspinSwitch)
        self.assertIsNotNone(st)
        self.assertIsNotNone(sw)
        self.assertIs(st._attr_entity_category, EntityCategory.DIAGNOSTIC)
        self.assertIs(sw._attr_entity_category, EntityCategory.CONFIG)
        self.assertEqual((st.unique_id, sw.unique_id), ("dev123abc456_sendspin_status", "dev123abc456_sendspin_player"))

    async def test_entities_appear_when_the_group_arrives_later_and_are_not_duplicated(self):
        coord, add_sensor, add_switch = await self.setup()
        coord.on_state("sendspin", GROUP)
        coord.on_state("sendspin", {**GROUP, "status": "connected"})
        self.assertEqual(len([e for e in add_sensor.entities if isinstance(e, sensor.SendspinStatusSensor)]), 1)
        self.assertEqual(len([e for e in add_switch.entities if isinstance(e, switch.SendspinSwitch)]), 1)

    async def test_the_sensor_follows_the_status_and_exposes_the_details(self):
        coord, add_sensor, _ = await self.setup(sendspin=GROUP)
        st = self.only(add_sensor, sensor.SendspinStatusSensor)
        self.assertEqual((st.native_value, st.available), ("waiting", True))
        coord.on_state("sendspin", {**GROUP, "status": "streaming", "server_name": "Music Assistant"})
        self.assertEqual(st.native_value, "streaming")
        self.assertEqual(st.extra_state_attributes, {"server_name": "Music Assistant", "port": 8928, "error": None, "player_name": "Box"})
        coord.on_state("sendspin", {"enabled": False, "status": "disabled", "name": "Box"})
        self.assertEqual((st.native_value, st.extra_state_attributes["port"]), ("disabled", None))
        coord.on_state("sendspin", {**GROUP, "status": "error", "error": "no free port"})
        self.assertEqual((st.native_value, st.extra_state_attributes["error"]), ("error", "no free port"))
        for option in ("disabled", "waiting", "connected", "streaming", "error"):
            self.assertIn(option, st._attr_options)

    async def test_a_status_this_version_does_not_know_is_shown_as_an_error(self):
        coord, add_sensor, _ = await self.setup(sendspin=GROUP)
        coord.on_state("sendspin", {**GROUP, "status": "exploding"})
        self.assertEqual(self.only(add_sensor, sensor.SendspinStatusSensor).native_value, "error")

    async def test_the_switch_reads_the_device_state_and_turns_on_and_off_through_the_command(self):
        coord, _, add_switch = await self.setup(sendspin=GROUP)
        sw = self.only(add_switch, switch.SendspinSwitch)
        self.assertEqual((sw.is_on, sw.available), (True, True))
        await sw.async_turn_off()
        self.assertTrue(sw.is_on)                                     # not optimistic: the device has not reported yet
        coord.on_state("sendspin", {"enabled": False, "status": "disabled", "name": "Box"})
        self.assertFalse(sw.is_on)
        await sw.async_turn_on()
        self.assertEqual(coord.client.calls, [("sendspin_enable", {"enabled": False}), ("sendspin_enable", {"enabled": True})])

    async def test_both_are_unavailable_when_the_device_is_offline_or_asleep_or_the_group_is_gone(self):
        coord, add_sensor, add_switch = await self.setup(sendspin=GROUP)
        st, sw = self.only(add_sensor, sensor.SendspinStatusSensor), self.only(add_switch, switch.SendspinSwitch)
        coord.on_event("going_to_sleep", {"reason": "suspend"})
        self.assertEqual((st.available, sw.available), (False, False))
        self.assertIsNone(coord.sendspin)
        coord.on_connection(False)
        self.assertEqual((st.available, sw.available), (False, False))

    async def test_a_failed_command_is_a_home_assistant_error(self):
        coord, _, add_switch = await self.setup(sendspin=GROUP)
        sw = self.only(add_switch, switch.SendspinSwitch)
        coord.client.connected = False
        with self.assertRaises(HomeAssistantError):
            await sw.async_turn_off()

    async def test_a_rejected_command_shows_the_devices_message(self):
        coord, _, add_switch = await self.setup(sendspin=GROUP)
        sw = self.only(add_switch, switch.SendspinSwitch)

        async def refuse(name, data=None):
            raise CommandError("internal", "could not save the setting")
        coord.client.command = refuse
        with self.assertRaises(HomeAssistantError) as cm:
            await sw.async_turn_on()
        self.assertIn("could not save", str(cm.exception))

    async def test_the_tracking_is_released_on_unload(self):
        coord = make_coordinator()
        connect(coord)
        entry = ConfigEntry()
        entry.runtime_data = coord
        entry.unloaders = []
        entry.async_on_unload = entry.unloaders.append
        add = Collector()
        await switch.async_setup_entry(coord.hass, entry, add)
        for unload in entry.unloaders:
            unload()
        coord.on_state("sendspin", GROUP)
        self.assertIsNone(self.only(add, switch.SendspinSwitch))     # nothing is added after unload

if __name__ == "__main__":
    unittest.main()
