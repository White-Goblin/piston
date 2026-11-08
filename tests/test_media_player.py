import unittest

from datetime import datetime, timezone

from homeassistant.components.media_player import MediaPlayerEntityFeature as F, MediaPlayerState as S, MediaType
from homeassistant.exceptions import HomeAssistantError

from custom_components.piston.client import CommandError
from custom_components.piston.media_player import DeckyMediaPlayer
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

    async def test_paused_app_shows_paused_so_the_play_button_appears(self):
        p, coord = player(apps={"apps": APPS, "running": True, "appid": "250900", "name": "Isaac", "playback": "playing"})
        self.assertEqual(p.state, S.PLAYING)
        coord.on_state("apps", {"apps": APPS, "running": True, "appid": "250900", "name": "Isaac", "playback": "paused"})
        self.assertEqual((p.state, p.extra_state_attributes["playback"]), (S.PAUSED, "paused"))
        coord.on_state("apps", {"apps": APPS, "running": True, "appid": "250900", "name": "Isaac", "playback": "unknown"})
        self.assertEqual(p.state, S.PLAYING)  # an app that reports nothing is assumed to be playing
        coord.on_state("apps", {"apps": APPS, "running": False, "playback": "paused"})
        self.assertEqual(p.state, S.IDLE)

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


MEDIA = {"title": "Snowplow Struttin'", "artist": "Thomas & Friends", "album": "Season 2", "kind": "video", "source": "mpris",
         "duration": 675.107, "position": 12.5, "position_at": 1_762_000_000.25, "art": "abc123"}
RUNNING = {"apps": APPS, "running": True, "appid": "2451505133", "name": "Jellyfin", "screen": "game", "playback": "playing"}


class NowPlayingTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_property_comes_from_the_media_group(self):
        p, _ = player(apps=RUNNING, media=MEDIA)
        self.assertEqual((p.media_title, p.media_artist, p.media_album_name), ("Snowplow Struttin'", "Thomas & Friends", "Season 2"))
        self.assertEqual((p.media_content_type, p.media_duration, p.media_position), (MediaType.VIDEO, 675, 12))
        self.assertEqual(p.media_position_updated_at, datetime.fromtimestamp(1_762_000_000.25, tz=timezone.utc))
        self.assertEqual(p.extra_state_attributes["media_source"], "mpris")
        self.assertEqual(p.app_name, "Jellyfin")

    async def test_the_steam_fallback_is_an_app(self):
        p, _ = player(apps=RUNNING, media={"title": "Jellyfin", "kind": "app", "source": "steam"})
        self.assertEqual((p.media_title, p.media_content_type, p.media_artist), ("Jellyfin", MediaType.APP, None))

    async def test_a_group_without_duration_has_no_progress(self):
        p, _ = player(apps=RUNNING, media={"title": "Live", "kind": "video", "source": "mpris"})
        self.assertEqual((p.media_duration, p.media_position, p.media_position_updated_at), (None, None, None))
        p, _ = player(apps=RUNNING, media={"title": "x", "position": 5.0, "position_at": 1.0})  # a position alone is not shown
        self.assertEqual((p.media_position, p.media_position_updated_at), (None, None))

    async def test_no_media_group_behaves_as_before(self):
        p, coord = player(apps=RUNNING)
        self.assertEqual(p.state, S.PLAYING)
        self.assertEqual((p.media_title, p.media_artist, p.media_album_name, p.media_content_type, p.media_duration,
                          p.media_position, p.media_position_updated_at), ("Jellyfin", None, None, None, None, None, None))
        self.assertIsNone(p.extra_state_attributes["media_source"])
        coord.on_state("media", {})
        self.assertEqual(p.media_title, "Jellyfin")

    async def test_desktop_mode_media_sets_the_state(self):
        idle = {"apps": APPS, "running": False, "screen": "desktop", "playback": "playing"}
        p, coord = player(apps=idle, media=MEDIA)
        self.assertEqual(p.state, S.PLAYING)
        coord.on_state("apps", {**idle, "playback": "paused"})
        self.assertEqual(p.state, S.PAUSED)
        coord.on_state("apps", {**idle, "playback": "unknown"})
        self.assertEqual(p.state, S.IDLE)
        coord.on_state("apps", idle)
        coord.on_state("media", {})
        self.assertEqual(p.state, S.IDLE)  # a stale playback status without media is ignored
        self.assertIsNone(p.source)

    async def test_the_media_group_is_gone_after_a_disconnect(self):
        p, coord = player(apps=RUNNING, media=MEDIA)
        coord.on_connection(False)
        self.assertIsNone(p.media_artist)

    async def test_seek_is_offered_only_with_a_known_duration(self):
        p, _ = player(apps=RUNNING, media=MEDIA)
        self.assertTrue(p.supported_features & F.SEEK)
        p, _ = player(apps=RUNNING, media={"title": "Live", "kind": "video", "source": "mpris"})
        self.assertFalse(p.supported_features & F.SEEK)
        p, _ = player(apps=RUNNING)
        self.assertFalse(p.supported_features & F.SEEK)


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

    async def test_seek_sends_the_absolute_position(self):
        p, coord = player(apps=RUNNING, media=MEDIA)
        await p.async_media_seek(100)
        self.assertEqual(self.calls(coord)[-1], ("media_seek", {"position": 100.0}))

    async def test_a_seek_failure_becomes_a_user_error(self):
        p, coord = player(apps=RUNNING, media=MEDIA)
        coord.client.error = CommandError("unsupported", "unknown command media_seek")
        with self.assertRaises(HomeAssistantError):
            await p.async_media_seek(100)

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

