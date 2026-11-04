import unittest

from homeassistant.core import HomeAssistant

from custom_components.piston import update_logic as ul
from custom_components.piston.coordinator import DeckyCoordinator
from custom_components.piston.switch import AutoUpdateSwitch
from custom_components.piston.update import PluginUpdate

from .entityutil import FakeClient
from .test_coordinator import entry

SHA = "d" * 64


class UpdateClient(FakeClient):
    lane_connected = False

    def __init__(self):
        super().__init__()
        self.sent = []

    async def send_update(self, version, url, sha256):
        self.sent.append((version, url, sha256))


class UpdateEntityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def fetch_text(url):
            return f"{SHA}  {ul.zip_name('0.4.1')}\n"

        self.coord = DeckyCoordinator(HomeAssistant(), entry(), off_timeout=3600, target_version="0.4.1", fetch_text=fetch_text)
        self.addCleanup(self.coord.shutdown)
        self.coord.client = UpdateClient()
        self.entity = PluginUpdate(self.coord)
        self.switch = AutoUpdateSwitch(self.coord)

    async def settle(self):
        if self.coord._decide_task is not None:
            await self.coord._decide_task

    def hello(self, version, skew=None):
        self.coord.on_hello({"type": "hello", "plugin_version": version, "lane": 1}, skew)

    async def test_installed_latest_and_release_page_follow_the_coordinator(self):
        self.coord.on_connection(True)
        self.coord.set_auto_update(False)
        self.hello("0.4.0")
        await self.settle()
        e = self.entity
        self.assertEqual((e.installed_version, e.latest_version, e.in_progress), ("0.4.0", "0.4.1", False))
        self.assertTrue(e.release_url.endswith("/releases/tag/v0.4.1"))
        self.assertEqual(e.extra_state_attributes, {"update_state": "available"})

    async def test_in_progress_and_last_failure_are_shown(self):
        self.coord.on_connection(True)
        self.hello("0.4.0")
        self.coord.on_update_status({"type": "update_status", "state": "downloading", "version": "0.4.1"})
        self.assertTrue(self.entity.in_progress)
        self.assertEqual(self.entity.extra_state_attributes["update_state"], "in_progress")
        self.coord.on_update_status({"type": "update_status", "state": "failed", "version": "0.4.1", "reason": "hash"})
        self.assertFalse(self.entity.in_progress)
        self.assertEqual(self.entity.extra_state_attributes, {"update_state": "failed", "last_failure": {"version": "0.4.1", "reason": "hash"}})

    async def test_a_plugin_the_integration_cannot_update_is_not_shown_as_an_update(self):
        self.coord.on_connection(True)
        self.coord.on_hello({"type": "hello"}, None)                      # no lane
        await self.settle()
        self.assertEqual((self.entity.installed_version, self.entity.latest_version), (None, None))
        self.assertEqual(self.entity.extra_state_attributes["update_state"], "no_lane")
        self.coord.target_version = "0.3.0"
        self.hello("0.4.0")
        await self.settle()
        self.assertEqual((self.entity.installed_version, self.entity.latest_version), ("0.4.0", "0.4.0"))

    async def test_available_when_connected_or_on_the_lane_but_not_when_away(self):
        self.assertFalse(self.entity.available)
        self.coord.on_connection(True)
        self.assertTrue(self.entity.available)
        self.coord.on_connection(False)
        self.assertFalse(self.entity.available)
        self.coord.skewed = "old"
        self.coord.client.lane_connected = True
        self.assertTrue(self.entity.available)
        self.coord.on_lane_closed()
        self.coord.client.lane_connected = False
        self.assertFalse(self.entity.available)

    async def test_install_sends_one_update_even_with_auto_off(self):
        self.coord.on_connection(True)
        self.coord.set_auto_update(False)
        self.hello("0.4.0")
        await self.settle()
        await self.entity.async_install(None, False)
        self.assertEqual(self.coord.client.sent, [("0.4.1", ul.asset_url("0.4.1"), SHA)])

    async def test_the_switch_is_a_config_setting_that_is_always_available(self):
        self.assertEqual(self.switch._attr_entity_category, "config")
        self.assertTrue(self.switch.is_on and self.switch.available)
        await self.switch.async_turn_off()
        self.assertFalse(self.switch.is_on)
        await self.switch.async_turn_on()
        self.assertTrue(self.switch.is_on)

    async def test_turning_the_switch_on_sends_a_pending_update(self):
        self.coord.on_connection(True)
        self.coord.set_auto_update(False)
        self.hello("0.4.0")
        await self.settle()
        await self.switch.async_turn_on()
        await self.settle()
        self.assertEqual(len(self.coord.client.sent), 1)

    async def test_install_while_the_device_is_away_is_an_error_the_user_sees(self):
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.piston.client import DeviceOffline
        self.coord.on_connection(True)
        self.hello("0.4.0")
        await self.settle()

        async def offline(*a):
            raise DeviceOffline("gone")

        self.coord.client.send_update = offline
        with self.assertRaises(HomeAssistantError):
            await self.entity.async_install(None, False)

    async def test_install_without_a_readable_checksum_is_an_error_the_user_sees(self):
        from homeassistant.exceptions import HomeAssistantError

        async def nothing(url):
            raise OSError("404")

        self.coord._fetch_text = nothing
        self.coord.on_connection(True)
        self.hello("0.4.0")
        await self.settle()
        with self.assertRaises(HomeAssistantError):
            await self.entity.async_install(None, False)
        self.assertEqual(self.coord.client.sent, [])


    # --- while Decky Loader's dialog waits for the user, the entity says what to do ---

    async def test_the_entity_asks_for_the_approval_or_for_game_mode(self):
        self.coord.on_connection(True)
        self.hello("0.4.0")
        await self.settle()
        self.assertIsNone(self.entity.release_summary)
        self.coord.on_update_status({"type": "update_status", "state": "waiting_for_confirmation", "version": "0.4.1"})
        self.assertTrue(self.entity.in_progress)
        self.assertEqual(self.entity.extra_state_attributes["update_state"], "waiting_for_confirmation")
        self.assertIn("Decky Loader dialog", self.entity.release_summary)
        self.assertLessEqual(len(self.entity.release_summary), 255)
        self.coord.on_update_status({"type": "update_status", "state": "waiting_for_ui", "version": "0.4.1"})
        self.assertEqual(self.entity.extra_state_attributes["update_state"], "waiting_for_ui")
        self.assertIn("game mode", self.entity.release_summary)
        self.coord.on_update_status({"type": "update_status", "state": "downloading", "version": "0.4.1"})
        self.assertIsNone(self.entity.release_summary)
        self.assertEqual(self.entity.extra_state_attributes["update_state"], "in_progress")

    async def test_a_dialog_that_was_not_approved_is_a_failure_with_its_reason(self):
        self.coord.on_connection(True)
        self.hello("0.4.0")
        await self.settle()
        self.coord.on_update_status({"type": "update_status", "state": "failed", "version": "0.4.1", "reason": "not_confirmed"})
        self.assertEqual(self.entity.extra_state_attributes["update_state"], "failed")
        self.assertIsNone(self.entity.release_summary)
        self.hello("0.4.0")                                  # no automatic retry
        await self.settle()
        self.assertEqual(self.coord.client.sent, [("0.4.1", ul.asset_url("0.4.1"), SHA)])
        await self.entity.async_install(None, False)         # an Install press asks again
        self.assertEqual(len(self.coord.client.sent), 2)

    async def test_a_dropped_connection_while_the_dialog_waits_is_not_a_version_failure(self):
        self.coord.on_connection(True)
        self.hello("0.4.0")
        await self.settle()
        self.coord.on_update_status({"type": "update_status", "state": "waiting_for_confirmation", "version": "0.4.1"})
        self.hello("0.4.0")                                  # reconnected, still the old version: looks like a failed install
        self.assertEqual(self.coord.update_latch, {"version": "0.4.1", "reason": "version"})
        self.coord.on_update_status({"type": "update_status", "state": "waiting_for_confirmation", "version": "0.4.1"})
        self.assertIsNone(self.coord.update_latch)           # the device says it is still waiting
