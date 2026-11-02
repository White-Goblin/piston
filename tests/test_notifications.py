import unittest

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError

from custom_components.decky_home_assistant import event as event_platform, notify as notify_platform
from custom_components.decky_home_assistant.client import CommandError
from tests.ha_stubs import PLATFORM
from .entityutil import connect, make_coordinator
from .test_sensors import Collector


async def setup(platform):
    coord = make_coordinator()
    connect(coord)
    entry = ConfigEntry()
    entry.runtime_data = coord
    add = Collector()
    await platform.async_setup_entry(coord.hass, entry, add)
    return coord, add.entities[0]


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_is_registered_with_the_documented_fields(self):
        await setup(notify_platform)
        schema, func = PLATFORM.services["show_toast"]
        self.assertEqual(func, "async_show_toast")
        fields = {k.key: k.default for k in schema}
        self.assertEqual(set(fields), {"message", "title", "duration", "icon", "silent", "id"})
        self.assertEqual((fields["duration"], fields["silent"]), (5, False))

    async def test_send_message_becomes_a_toast_command(self):
        coord, ent = await setup(notify_platform)
        await ent.async_send_message("Hello", title="Doorbell")
        self.assertEqual(coord.client.calls, [("toast", {"message": "Hello", "title": "Doorbell", "duration": 5000, "silent": False})])

    async def test_show_toast_converts_seconds_and_keeps_options(self):
        coord, ent = await setup(notify_platform)
        await ent.async_show_toast(message="Motion", duration=2.5, icon="motion", silent=True, id="front")
        self.assertEqual(coord.client.calls[-1], ("toast", {"message": "Motion", "duration": 2500, "silent": True,
                                                           "icon": "motion", "id": "front"}))

    async def test_icons_in_the_schema_match_the_frontend(self):
        import re
        with open("src/notify/toasts.tsx") as f:
            frontend = set(re.findall(r"^\s{2}(\w+): <Fa", f.read(), re.M))
        self.assertEqual(set(notify_platform.ICONS), frontend)

    async def test_device_rejection_is_shown_to_the_user(self):
        coord, ent = await setup(notify_platform)
        coord.client.error = CommandError("unsupported", "this only works in game mode")
        with self.assertRaises(HomeAssistantError) as cm:
            await ent.async_send_message("x")
        self.assertIn("game mode", str(cm.exception))

    async def test_offline_notify_is_unavailable_and_fails_clearly(self):
        coord, ent = await setup(notify_platform)
        coord.on_connection(False)
        self.assertFalse(ent.available)
        coord.client.connected = False
        with self.assertRaises(HomeAssistantError):
            await ent.async_send_message("x")


class ToastClickTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_fires_the_event_with_the_toast_id(self):
        coord, ent = await setup(event_platform)
        await ent.async_added_to_hass()
        coord.on_event("toast_click", {"id": "front", "at": 1762000000.5})
        self.assertEqual(ent.triggered, [("clicked", {"id": "front", "clicked_at": 1762000000.5})])
        self.assertEqual(ent.writes, 1)

    async def test_click_without_an_id_has_none(self):
        coord, ent = await setup(event_platform)
        await ent.async_added_to_hass()
        coord.on_event("toast_click", {"id": "", "at": 1.0})
        self.assertIsNone(ent.triggered[0][1]["id"])

    async def test_stops_listening_when_removed(self):
        coord, ent = await setup(event_platform)
        await ent.async_added_to_hass()
        await ent.async_will_remove_from_hass()
        coord.on_event("toast_click", {"id": "a", "at": 1.0})
        self.assertEqual(getattr(ent, "triggered", []), [])

    async def test_event_types_and_unique_id(self):
        _, ent = await setup(event_platform)
        self.assertEqual((ent._attr_event_types, ent.unique_id), (["clicked"], "dev123abc456_toast_click"))
