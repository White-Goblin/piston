"""After a Home Assistant restart the entities exist from the stored structure, with no live values, until the device reports."""
import unittest
from unittest import mock

from homeassistant.components.media_player import MediaPlayerEntityFeature as F, MediaPlayerState as S
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

import custom_components.piston as integration
from custom_components.piston import config_flow
from custom_components.piston import binary_sensor, media_player, sensor
from custom_components.piston.store import restored_groups, stable_subset
from .entityutil import make_coordinator
from .stdlib_transport import StdTransport
from .test_sensors import Collector
from .test_store import LIVE

STORED = stable_subset(LIVE)


def restored_coordinator():
    coord = make_coordinator()
    coord.restore(restored_groups(STORED), None)
    return coord


async def create_entities(coord):
    entry = ConfigEntry()
    entry.runtime_data = coord
    entry.async_on_unload = lambda f: None
    adders = {name: Collector() for name in ("sensor", "binary_sensor", "media_player")}
    await sensor.async_setup_entry(coord.hass, entry, adders["sensor"])
    await binary_sensor.async_setup_entry(coord.hass, entry, adders["binary_sensor"])
    await media_player.async_setup_entry(coord.hass, entry, adders["media_player"])
    return adders


class RestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_restored_groups_have_the_shape_of_live_groups_and_are_flagged(self):
        coord = restored_coordinator()
        self.assertEqual(coord.groups["sensors"], {"cpu_load": None, "uptime": None})
        self.assertEqual(coord.groups["apps"], {"apps": [{"appid": "1", "name": "A"}]})
        self.assertEqual(coord.restored, set(STORED))
        self.assertEqual(coord.power, "off")                       # the power state is never restored

    async def test_the_first_live_frame_of_a_group_replaces_it_and_clears_only_its_flag(self):
        coord = restored_coordinator()
        coord.on_state("apps", {"apps": [], "running": False})
        self.assertNotIn("apps", coord.restored)
        self.assertIn("sensors", coord.restored)
        self.assertEqual(coord.groups["apps"], {"apps": [], "running": False})

    async def test_media_is_never_stored_or_restored(self):
        live = {**LIVE, "media": {"title": "Ep", "art": "abc", "kind": "video", "source": "mpris"}}
        self.assertNotIn("media", stable_subset(live))
        coord = make_coordinator()
        coord.restore({"media": {"title": "stale"}, **restored_groups(STORED)}, None)
        self.assertNotIn("media", coord.groups)
        self.assertNotIn("media", coord.restored)

    async def test_live_data_is_never_overwritten_by_a_restore(self):
        coord = make_coordinator()
        coord.on_state("audio", {"mode": "cec"})
        coord.restore(restored_groups(STORED), None)
        self.assertEqual(coord.groups["audio"], {"mode": "cec"})
        self.assertNotIn("audio", coord.restored)

    async def test_the_last_connection_time_is_restored_only_if_nothing_newer_is_known(self):
        from datetime import datetime, timezone
        old = datetime(2026, 11, 1, tzinfo=timezone.utc)
        coord = make_coordinator()
        coord.restore({}, old)
        self.assertEqual(coord.last_connected, old)
        coord.on_connection(True)
        newer = coord.last_connected
        coord.restore({}, old)
        self.assertEqual(coord.last_connected, newer)

    async def test_entities_exist_before_any_connection_with_no_values(self):
        coord = restored_coordinator()
        adders = await create_entities(coord)
        sensors = [e for e in adders["sensor"].entities if isinstance(e, sensor.DeckySensor)]
        self.assertEqual(sorted(e._key for e in sensors), ["cpu_load", "uptime"])
        self.assertTrue(all(e.native_value is None and not e.available for e in sensors))
        batteries = [e for e in adders["sensor"].entities if isinstance(e, sensor.ControllerBatterySensor)]
        self.assertEqual([(b._serial, b.native_value, b.available) for b in batteries], [("s1", None, False)])
        (player,) = adders["media_player"].entities
        self.assertEqual(player.source_list, ["Steam", "A"])        # the offered apps are known

    async def test_the_media_player_is_off_with_no_running_app_volume_or_source(self):
        coord = restored_coordinator()
        (player,) = (await create_entities(coord))["media_player"].entities
        self.assertEqual((player.state, player.available), (S.OFF, True))
        self.assertEqual((player.source, player.media_title, player.app_name, player.volume_level, player.is_volume_muted),
                         (None, None, None, None, None))
        self.assertTrue(player.supported_features & F.VOLUME_SET)   # the audio mode is a stable fact

    async def test_going_live_replaces_the_restored_values(self):
        coord = restored_coordinator()
        (player,) = (await create_entities(coord))["media_player"].entities
        sensors = [e for e in (await create_entities(coord))["sensor"].entities if isinstance(e, sensor.DeckySensor)]
        coord.on_connection(True)
        self.assertFalse(sensors[0].available)                      # online, but its group is still the restored one
        coord.on_state("apps", {"apps": [{"appid": "1", "name": "A"}], "running": True, "appid": "1", "name": "A", "screen": "game"})
        coord.on_state("sensors", {"cpu_load": 7, "uptime": 99})
        self.assertEqual((player.source, player.state), ("A", S.PLAYING))
        by_key = {e._key: e for e in sensors}
        self.assertEqual((by_key["cpu_load"].native_value, by_key["cpu_load"].available), (7, True))

    async def test_nothing_stored_behaves_as_before(self):
        coord = make_coordinator()
        coord.restore(restored_groups({}), None)
        self.assertEqual((coord.groups, coord.restored), ({}, set()))


class SetupRestoreTests(unittest.IsolatedAsyncioTestCase):
    """async_setup_entry loads the store before the platforms are created and before the client starts."""

    async def asyncSetUp(self):
        Store.reset()
        patcher = mock.patch.object(config_flow, "make_transport", lambda hass: StdTransport())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hass = HomeAssistant()
        self.entry = ConfigEntry(data={"device_id": "dev123abc456", "secret": "00" * 32, "host": "127.0.0.1", "port": 1,
                                       "name": "Box"}, unique_id="dev123abc456", entry_id="e1")
        self.seen = []

        async def forward(entry, platforms):
            self.seen.append((dict(entry.runtime_data.groups), set(entry.runtime_data.restored), entry.runtime_data.connected))

        self.hass.config_entries.async_forward_entry_setups = forward
        self.addAsyncCleanup(integration.async_unload_entry, self.hass, self.entry)

    async def test_stored_structure_is_in_place_before_the_platforms_are_set_up(self):
        Store.files["piston.e1"] = {"version": 1, "data": {"groups": STORED, "last_connected": "2026-11-03T08:00:00+00:00"}}
        await integration.async_setup_entry(self.hass, self.entry)
        groups, restored, connected = self.seen[0]
        self.assertEqual(sorted(groups), ["apps", "audio", "controllers", "device", "sensors"])
        self.assertEqual(restored, set(groups))
        self.assertFalse(connected)
        self.assertEqual(self.entry.runtime_data.last_connected.isoformat(), "2026-11-03T08:00:00+00:00")

    async def test_without_a_stored_file_startup_is_unchanged(self):
        await integration.async_setup_entry(self.hass, self.entry)
        self.assertEqual(self.seen[0][:2], ({}, set()))

    async def test_a_corrupt_file_is_ignored_at_startup(self):
        Store.files["piston.e1"] = ValueError("not json")
        with self.assertLogs("custom_components.piston.store", "WARNING"):
            self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))
        self.assertEqual(self.seen[0][:2], ({}, set()))


if __name__ == "__main__":
    unittest.main()
