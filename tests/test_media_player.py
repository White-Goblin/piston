import unittest

from homeassistant.components.media_player import MediaPlayerEntityFeature as F, MediaPlayerState as S
from homeassistant.exceptions import HomeAssistantError

from custom_components.decky_home_assistant.client import CommandError
from custom_components.decky_home_assistant.media_player import DeckyMediaPlayer
from .entityutil import connect, make_coordinator

APPS = [{"appid": "2451505133", "name": "Jellyfin", "kind": "shortcut"}, {"appid": "250900", "name": "Isaac", "kind": "steam"}]


def player(options=None, **groups):
    coord = make_coordinator(options)
    connect(coord, **groups)
    return DeckyMediaPlayer(coord), coord


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_standby_idle_playing(self):
        p, coord = player(apps={"apps": APPS, "running": False})
        self.assertEqual(p.state, S.IDLE)
        coord.on_state("apps", {"apps": APPS, "running": True, "appid": "250900", "name": "Isaac", "screen": "game"})
        self.assertEqual(p.state, S.PLAYING)
        coord.on_event("going_to_sleep", {})
        coord.on_connection(False)
        self.assertEqual(p.state, S.STANDBY)
        fresh, _ = player()
        fresh.coordinator.power = "off"
        self.assertEqual(fresh.state, S.OFF)
        self.assertTrue(fresh.available)

    async def test_sources_and_current_source(self):
        p, coord = player(apps={"apps": APPS, "running": False})
        self.assertEqual(p.source_list, ["Steam", "Jellyfin", "Isaac"])
        self.assertEqual(p.source, "Steam")
        coord.on_state("apps", {"apps": APPS, "running": True, "appid": "250900", "name": "Isaac", "screen": "game"})
        self.assertEqual((p.source, p.media_title, p.app_name), ("Isaac", "Isaac", "Isaac"))
        self.assertEqual(p.extra_state_attributes["steam_screen"], "game")
        coord.on_state("apps", {"apps": APPS, "running": True, "appid": "9", "name": "Other", "screen": "game"})
        self.assertIsNone(p.source)

    async def test_duplicate_source_names_stay_selectable(self):
        dup = [{"appid": "1", "name": "Game"}, {"appid": "2", "name": "Game"}, {"appid": "3", "name": "Steam"}]
        p, _ = player(apps={"apps": dup})
        self.assertEqual(p.source_list, ["Steam", "Game (1)", "Game (2)", "Steam (3)"])

    async def test_features_by_audio_mode(self):
        p, coord = player(audio={"mode": "cec"})
        self.assertFalse(p.supported_features & F.VOLUME_SET)
        self.assertTrue(p.supported_features & F.VOLUME_STEP and p.supported_features & F.SELECT_SOURCE)
        coord.on_state("audio", {"mode": "system", "volume": 40, "muted": True})
        self.assertTrue(p.supported_features & F.VOLUME_SET)
        self.assertEqual((p.volume_level, p.is_volume_muted), (0.4, True))

    async def test_cec_has_no_level_and_assumes_mute(self):
        p, coord = player(audio={"mode": "cec"})
        self.assertIsNone(p.volume_level)
        self.assertFalse(p.is_volume_muted)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def calls(self, coord):
        return coord.client.calls

    async def test_select_source_launches_or_quits(self):
        p, coord = player(apps={"apps": APPS})
        await p.async_select_source("Isaac")
        await p.async_select_source("Steam")
        self.assertEqual(self.calls(coord)[-2:], [("launch", {"appid": "250900"}), ("quit", None)])
        with self.assertRaises(HomeAssistantError):
            await p.async_select_source("Nope")

    async def test_turn_off_follows_the_option(self):
        p, coord = player()
        await p.async_turn_off()
        p2, coord2 = player({"turn_off": "shutdown"})
        await p2.async_turn_off()
        self.assertEqual(self.calls(coord)[-1], ("power", {"action": "suspend"}))
        self.assertEqual(self.calls(coord2)[-1], ("power", {"action": "shutdown"}))

    async def test_transport_keys(self):
        p, coord = player()
        await p.async_media_play()
        await p.async_media_pause()
        await p.async_media_play_pause()
        await p.async_media_next_track()
        await p.async_media_previous_track()
        self.assertEqual([c[1]["key"] for c in self.calls(coord)], ["play", "pause", "play_pause", "next", "previous"])

    async def test_cec_volume_and_mute_toggle(self):
        p, coord = player(audio={"mode": "cec"})
        await p.async_volume_up()
        await p.async_volume_down()
        await p.async_mute_volume(True)
        await p.async_mute_volume(True)   # already assumed muted: nothing sent
        await p.async_mute_volume(False)
        self.assertEqual([c[1]["action"] for c in self.calls(coord)], ["volume_up", "volume_down", "mute", "mute"])
        self.assertFalse(p.is_volume_muted)

    async def test_system_volume(self):
        p, coord = player(audio={"mode": "system", "volume": 40, "muted": False})
        await p.async_volume_up()
        await p.async_volume_down()
        await p.async_set_volume_level(0.73)
        await p.async_mute_volume(True)
        self.assertEqual(self.calls(coord), [("audio", {"action": "set_volume", "value": 45}),
                                             ("audio", {"action": "set_volume", "value": 35}),
                                             ("audio", {"action": "set_volume", "value": 73}),
                                             ("audio", {"action": "set_mute", "value": True})])

    async def test_system_volume_clamps(self):
        p, coord = player(audio={"mode": "system", "volume": 98, "muted": False})
        await p.async_volume_up()
        self.assertEqual(self.calls(coord)[-1][1]["value"], 100)

    async def test_offline_and_device_errors_become_user_errors(self):
        p, coord = player()
        coord.client.connected = False
        with self.assertRaises(HomeAssistantError) as cm:
            await p.async_select_source("Steam")
        self.assertIn("offline", str(cm.exception))
        coord.client.connected = True
        coord.client.error = CommandError("not_running", "nothing is playing")
        with self.assertRaises(HomeAssistantError) as cm:
            await p.async_media_play_pause()
        self.assertEqual(str(cm.exception), "nothing is playing")
