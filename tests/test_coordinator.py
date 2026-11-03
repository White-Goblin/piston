import asyncio
import unittest
from unittest import mock

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.issue_registry import IssueRegistry

import custom_components.decky_home_assistant as integration
from custom_components.decky_home_assistant import client as c, config_flow as cf
from custom_components.decky_home_assistant.binary_sensor import ConnectivitySensor
from custom_components.decky_home_assistant.coordinator import DeckyCoordinator
from decky_ha.pairing import PairingManager
from decky_ha.server import ProtocolServer
from .stdlib_transport import StdTransport

SECRET = b"s" * 32


def entry(**extra):
    return ConfigEntry(data={"device_id": "dev123abc456", "secret": SECRET.hex(), "host": "127.0.0.1", "port": 1,
                             "name": "Box", **extra}, options=extra.pop("options", {}), unique_id="dev123abc456",
                       entry_id="e1")


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant()
        self.coord = DeckyCoordinator(self.hass, entry(), off_timeout=0.1)
        self.addCleanup(self.coord.shutdown)
        self.notified = 0
        self.coord.async_add_listener(lambda: setattr(self, "notified", self.notified + 1))

    async def test_never_seen_device_is_off_and_entities_unavailable(self):
        self.assertEqual((self.coord.power, self.coord.connected), ("off", False))
        sensor = ConnectivitySensor(self.coord)
        self.assertFalse(sensor.is_on)
        self.assertTrue(sensor.available)  # connectivity stays available so it can say "off"

    async def test_connect_disconnect_marks_entities_and_connectivity(self):
        sensor = ConnectivitySensor(self.coord)
        self.coord.on_connection(True)
        self.assertEqual((self.coord.power, sensor.is_on), ("on", True))
        self.coord.on_connection(False)
        self.assertFalse(sensor.is_on)

    # --- power model: shutdown announcement, silent loss, logging, last_connected ----------

    LOGGER = "custom_components.decky_home_assistant.coordinator"

    async def test_a_shutdown_announcement_is_off_at_once_without_waiting_for_the_timeout(self):
        self.coord.on_connection(True)
        self.coord.on_event("shutting_down", {"reason": "shutdown"})
        self.assertEqual((self.coord.power, self.coord.sleeping), ("off", False))
        self.assertIsNone(self.coord._off_handle)
        self.coord.on_connection(False)                    # the connection then drops: still off, no timer to start
        self.assertEqual(self.coord.power, "off")
        self.assertIsNone(self.coord._off_handle)

    async def test_a_shutdown_while_asleep_is_off(self):
        self.coord.on_connection(True)
        self.coord.on_event("going_to_sleep", {})
        self.coord.on_event("shutting_down", {"reason": "restart"})
        self.assertEqual((self.coord.power, self.coord.sleeping), ("off", False))

    async def test_a_connection_after_a_shutdown_is_on_again(self):
        self.coord.on_event("shutting_down", {"reason": "shutdown"})
        self.coord.on_connection(True)
        self.assertEqual(self.coord.power, "on")

    async def test_standby_after_a_sleep_announcement_does_not_start_the_off_timer(self):
        self.coord.on_connection(True)
        self.coord.on_event("going_to_sleep", {})
        self.coord.on_connection(False)
        self.assertEqual(self.coord.power, "standby")
        self.assertIsNone(self.coord._off_handle)

    async def test_a_silent_drop_becomes_off_after_the_timeout(self):
        self.coord.on_connection(True)
        self.coord.on_connection(False)
        self.assertEqual(self.coord.power, "on")           # kept until the timeout
        self.assertIsNotNone(self.coord._off_handle)
        await asyncio.sleep(0.25)
        self.assertEqual(self.coord.power, "off")

    async def test_unavailable_and_back_online_are_each_logged_once(self):
        with self.assertLogs(self.LOGGER, "INFO") as logs:
            self.coord.on_connection(True)
            self.coord.on_event("going_to_sleep", {})      # unavailable (standby)
            self.coord.on_connection(False)
            self.coord.on_event("shutting_down", {})       # standby -> off: not logged again
            await asyncio.sleep(0.25)
            self.coord.on_connection(True)                 # back online
            self.coord.on_connection(True)
        messages = [m.split(":", 2)[-1] for m in logs.output]
        self.assertEqual(sum("unavailable" in m for m in messages), 1)
        self.assertEqual(sum("back online" in m for m in messages), 1)

    async def test_a_second_outage_is_logged_again(self):
        with self.assertLogs(self.LOGGER, "INFO") as logs:
            for _ in range(2):
                self.coord.on_connection(True)
                self.coord.on_event("going_to_sleep", {})
        self.assertEqual(sum("unavailable" in m for m in logs.output), 2)
        self.assertEqual(sum("back online" in m for m in logs.output), 1)

    async def test_last_connected_is_the_last_moment_there_was_a_connection(self):
        from datetime import datetime, timezone
        clock = [datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)]
        coord = DeckyCoordinator(self.hass, entry(), off_timeout=0.1, now=lambda: clock[0])
        self.addCleanup(coord.shutdown)
        self.assertIsNone(coord.last_connected)
        coord.on_connection(True)
        self.assertEqual(coord.last_connected, clock[0])
        clock[0] = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)      # three days connected, then it drops
        coord.on_connection(False)
        self.assertEqual(coord.last_connected, clock[0])                  # not the start of the connection

    async def test_sleep_announcement_gives_standby_immediately(self):
        self.coord.on_connection(True)
        self.coord.on_event("going_to_sleep", {})
        self.coord.on_connection(False)
        self.assertEqual(self.coord.power, "standby")
        await asyncio.sleep(0.2)
        self.assertEqual(self.coord.power, "standby")  # the timeout does not turn standby into off

    async def test_standby_shows_at_once_even_if_the_connection_survives_the_suspend(self):
        self.coord.on_connection(True)
        self.coord.on_event("going_to_sleep", {})
        self.assertEqual((self.coord.power, self.coord.connected), ("standby", True))
        self.coord.on_event("resumed", {})
        self.assertEqual((self.coord.power, self.coord.sleeping), ("on", False))

    async def test_entities_and_connectivity_go_unavailable_while_the_device_sleeps(self):
        from custom_components.decky_home_assistant.button import DeckyPowerButton
        self.coord.on_connection(True)
        sensor, button = ConnectivitySensor(self.coord), DeckyPowerButton(self.coord, "restart_steam", "restart_steam")
        self.assertEqual((sensor.is_on, button.available), (True, True))
        self.coord.on_event("going_to_sleep", {})
        self.assertEqual((sensor.is_on, button.available, self.coord.online), (False, False, False))
        self.coord.on_event("resumed", {})
        self.assertEqual((sensor.is_on, button.available), (True, True))

    async def test_silent_drop_keeps_state_until_the_timeout_then_off(self):
        self.coord.on_connection(True)
        self.coord.on_connection(False)
        self.assertEqual(self.coord.power, "on")
        await asyncio.sleep(0.25)
        self.assertEqual(self.coord.power, "off")

    async def test_reconnect_cancels_the_timeout_and_clears_sleep(self):
        self.coord.on_connection(True)
        self.coord.on_event("going_to_sleep", {})
        self.coord.on_connection(False)
        self.coord.on_connection(True)
        self.assertEqual((self.coord.power, self.coord.sleeping), ("on", False))

    async def test_state_updates_notify_and_store_groups(self):
        before = self.notified
        self.coord.on_state("sensors", {"cpu_load": 1})
        self.assertEqual(self.coord.groups["sensors"], {"cpu_load": 1})
        self.assertEqual(self.notified, before + 1)

    async def test_mac_is_persisted_once_when_it_changes(self):
        saved = []
        coord = DeckyCoordinator(self.hass, entry(), persist_mac=saved.append)
        self.addCleanup(coord.shutdown)
        coord.on_state("device", {"mac": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(saved, ["aa:bb:cc:dd:ee:ff"])
        coord.entry.data["mac"] = "aa:bb:cc:dd:ee:ff"
        coord.on_state("device", {"mac": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual(len(saved), 1)

    async def test_event_listeners(self):
        got = []
        remove = self.coord.async_add_event_listener("toast_click", got.append)
        self.coord.on_event("toast_click", {"id": "a"})
        remove()
        self.coord.on_event("toast_click", {"id": "b"})
        self.assertEqual(got, [{"id": "a"}])

    async def test_problem_is_recorded_and_cleared_on_connect(self):
        self.coord.on_problem(c.VersionSkew("old"))
        self.assertEqual(self.coord.problem, "version_skew_old")
        self.coord.on_problem(c.AuthFailed("x"))
        self.assertEqual(self.coord.problem, "auth_failed")
        self.coord.on_connection(True)
        self.assertIsNone(self.coord.problem)

    async def test_entity_device_info_and_listener_registration(self):
        sensor = ConnectivitySensor(self.coord)
        self.assertEqual(sensor.unique_id, "dev123abc456_connectivity")
        self.assertEqual(sensor.device_info["identifiers"], {("decky_home_assistant", "dev123abc456")})
        self.assertNotIn("connections", sensor.device_info)
        self.coord.on_state("device", {"mac": "aa:bb"})
        self.assertEqual(ConnectivitySensor(self.coord).device_info["connections"], {("mac", "aa:bb")})
        await sensor.async_added_to_hass()
        self.coord.on_state("x", {})
        self.assertEqual(sensor.writes, 1)
        await sensor.async_will_remove_from_hass()
        self.coord.on_state("x", {})
        self.assertEqual(sensor.writes, 1)

    async def test_commands_without_a_client_fail_fast(self):
        with self.assertRaises(c.DeviceOffline):
            await self.coord.async_command("echo")


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Store.reset()
        self.box = {"secret": SECRET}
        self.mgr = PairingManager(lambda: self.box["secret"], lambda s: self.box.update(secret=s))
        self.received = []

        async def interval(data):
            self.received.append(data)
            return {}

        async def unpair(data):
            self.mgr.unpair()
            return {}

        self.server = ProtocolServer(device_id=lambda: "dev123abc456", secret=lambda: self.box["secret"], pairing=self.mgr,
                                     commands={"set_sensor_interval": interval, "unpair": unpair},
                                     snapshot=lambda: {"device": {"mac": "aa:bb:cc:dd:ee:ff"}}, host="127.0.0.1", port=0)
        await self.server.start()
        self.addAsyncCleanup(self.server.stop)
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(data={"device_id": "dev123abc456", "secret": SECRET.hex(), "host": "127.0.0.1",
                                       "port": self.server.port, "name": "Box"}, options={"sensor_interval": 60},
                                 unique_id="dev123abc456", entry_id="e1")
        self.hass.config_entries.entries.append(self.entry)
        patcher = mock.patch.object(cf, "make_transport", lambda hass: StdTransport())
        patcher.start()
        self.addCleanup(patcher.stop)
        IssueRegistry.created.clear()
        IssueRegistry.deleted.clear()

    async def wait_for(self, predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0.02)
        self.fail("timed out")

    async def test_setup_connects_applies_options_and_stores_the_mac(self):
        self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))
        self.addAsyncCleanup(integration.async_unload_entry, self.hass, self.entry)
        coord = self.entry.runtime_data
        await self.wait_for(lambda: self.received and self.entry.data.get("mac"))
        self.assertEqual(self.received, [{"seconds": 60}])
        self.assertEqual((coord.power, coord.connected), ("on", True))
        self.assertEqual(self.entry.data["mac"], "aa:bb:cc:dd:ee:ff")
        from custom_components.decky_home_assistant.const import PLATFORMS
        self.assertEqual(self.hass.config_entries.forwarded, [PLATFORMS])
        # a good connection clears any old version-skew repair issue
        self.assertEqual(sorted(IssueRegistry.deleted), ["version_skew_new_dev123abc456", "version_skew_old_dev123abc456"])

    async def test_changed_options_are_applied_to_the_connected_device(self):
        await integration.async_setup_entry(self.hass, self.entry)
        self.addAsyncCleanup(integration.async_unload_entry, self.hass, self.entry)
        await self.wait_for(lambda: self.received)
        self.entry.options = {"sensor_interval": 300}
        await integration._options_updated(self.hass, self.entry)
        self.assertEqual(self.received[-1], {"seconds": 300})

    async def test_wrong_secret_starts_reauth(self):
        self.box["secret"] = b"z" * 32
        await integration.async_setup_entry(self.hass, self.entry)
        self.addAsyncCleanup(integration.async_unload_entry, self.hass, self.entry)
        await self.wait_for(lambda: self.entry.reauth_started)
        self.assertEqual(self.entry.runtime_data.problem, "auth_failed")

    async def test_unload_stops_the_client(self):
        await integration.async_setup_entry(self.hass, self.entry)
        await self.wait_for(lambda: self.entry.runtime_data.connected)
        self.assertTrue(await integration.async_unload_entry(self.hass, self.entry))
        self.assertFalse(self.entry.runtime_data.client.connected)

    async def test_stable_state_is_stored_after_the_delay_and_not_for_every_reading(self):
        await integration.async_setup_entry(self.hass, self.entry)
        self.addAsyncCleanup(integration.async_unload_entry, self.hass, self.entry)
        coord = self.entry.runtime_data
        await self.wait_for(lambda: coord.connected and coord.groups.get("device"))
        coord.on_state("sensors", {"cpu_load": 1})
        delays = len(Store.delays)
        for load in range(2, 12):
            coord.on_state("sensors", {"cpu_load": load})        # readings change, the sensor keys do not
        self.assertEqual(len(Store.delays), delays)
        await coord.store._store.fire_delay()
        key, data = Store.writes[-1]
        self.assertEqual(key, "decky_home_assistant.e1")
        self.assertEqual(data["groups"]["sensors"], {"keys": ["cpu_load"]})
        self.assertEqual(data["groups"]["device"]["mac"], "aa:bb:cc:dd:ee:ff")

    async def test_unload_saves_at_once_while_the_connection_state_is_known(self):
        await integration.async_setup_entry(self.hass, self.entry)
        await self.wait_for(lambda: self.entry.runtime_data.connected)
        before = len(Store.writes)
        await integration.async_unload_entry(self.hass, self.entry)
        self.assertEqual(len(Store.writes), before + 1)
        self.assertIsNotNone(Store.writes[-1][1]["last_connected"])      # connected at that moment: now

    async def test_stopping_home_assistant_saves(self):
        await integration.async_setup_entry(self.hass, self.entry)
        self.addAsyncCleanup(integration.async_unload_entry, self.hass, self.entry)
        await self.wait_for(lambda: self.entry.runtime_data.connected)
        before = len(Store.writes)
        await self.hass.bus.fire("homeassistant_stop")
        self.assertEqual(len(Store.writes), before + 1)

    async def test_removing_the_entry_deletes_its_stored_state(self):
        Store.files["decky_home_assistant.e1"] = {"version": 1, "data": {"groups": {}}}
        await integration.async_remove_entry(self.hass, self.entry)
        self.assertNotIn("decky_home_assistant.e1", Store.files)

    async def test_removing_the_entry_also_removes_the_unreachable_issue(self):
        await integration.async_remove_entry(self.hass, self.entry)
        self.assertIn("device_unreachable_dev123abc456", IssueRegistry.deleted)

    async def test_setup_starts_the_unreachable_monitor(self):
        await integration.async_setup_entry(self.hass, self.entry)
        self.addAsyncCleanup(integration.async_unload_entry, self.hass, self.entry)
        self.assertIsNotNone(self.entry.runtime_data.unreachable)

    async def test_removing_the_entry_resets_the_device(self):
        await integration.async_remove_entry(self.hass, self.entry)
        self.assertEqual(self.box["secret"], b"")

    async def test_removing_an_unreachable_device_does_not_raise(self):
        self.entry.data["port"] = 1
        await integration.async_remove_entry(self.hass, self.entry)
        self.assertEqual(self.box["secret"], SECRET)


class VersionSkewIssueTests(SetupTests):
    async def test_version_skew_raises_a_repair_issue(self):
        original = integration.DeckyClient

        def skewed(*args, **kw):
            return original(*args, own=(9, 9), **kw)

        with mock.patch.object(integration, "DeckyClient", skewed):
            await integration.async_setup_entry(self.hass, self.entry)
        self.addAsyncCleanup(integration.async_unload_entry, self.hass, self.entry)
        await self.wait_for(lambda: IssueRegistry.created)
        issue_id, kw = IssueRegistry.created[0]
        self.assertEqual((issue_id, kw["translation_key"]), ("version_skew_old_dev123abc456", "version_skew_old"))
        self.assertEqual(self.entry.runtime_data.problem, "version_skew_old")
