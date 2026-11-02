import asyncio
import unittest

from decky_ha.media_keys import KEY_CODES, MPRIS_METHODS, MediaKeyMixin
from decky_ha.proto import messages as m


class Host(MediaKeyMixin):
    def __init__(self, players=None, running=True):
        self.players = players or {}
        self.app_state = {"running": running, "playback": "unknown"}
        self.calls, self.keys, self.fail_uinput = [], [], False

    def _set_app_state(self, **changes):
        self.app_state.update(changes)

    def _busctl(self, *args):
        self.calls.append(args)
        if args[0] == "list":
            return "\n".join(f"{n} 123 x deck :1.1 u - -" for n in ["org.freedesktop.DBus", *self.players])
        if args[0] == "get-property":
            return f's "{self.players[args[1]]}"'
        if args[0] == "call" and self.players.get(args[1]) == "broken":
            raise RuntimeError("no reply")
        return ""

    def _press_virtual_key(self, key):
        if self.fail_uinput:
            raise PermissionError("denied")
        self.keys.append(key)


class MediaKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_mpris_player_is_used_and_playing_one_wins(self):
        h = Host({"org.mpris.MediaPlayer2.paused": "Paused", "org.mpris.MediaPlayer2.jellyfin": "Playing"})
        self.assertEqual(await h._cmd_media_key({"key": "play_pause"}), {"via": "mpris", "player": "jellyfin"})
        self.assertEqual([c for c in h.calls if c[0] == "call"][-1], ("call", "org.mpris.MediaPlayer2.jellyfin", "/org/mpris/MediaPlayer2",
                                       "org.mpris.MediaPlayer2.Player", "PlayPause"))
        self.assertEqual(h.keys, [])

    async def test_every_key_maps_to_a_method_and_a_key_code(self):
        self.assertEqual(set(MPRIS_METHODS), set(KEY_CODES))
        for key, method in MPRIS_METHODS.items():
            h = Host({"org.mpris.MediaPlayer2.p": "Playing"})
            await h._cmd_media_key({"key": key})
            self.assertEqual([c for c in h.calls if c[0] == "call"][-1][-1], method)

    async def test_falls_back_to_the_virtual_key_when_an_app_runs(self):
        h = Host()
        self.assertEqual(await h._cmd_media_key({"key": "next"}), {"via": "uinput"})
        self.assertEqual(h.keys, ["next"])

    async def test_broken_player_falls_back(self):
        h = Host({"org.mpris.MediaPlayer2.x": "broken"})
        self.assertEqual((await h._cmd_media_key({"key": "pause"}))["via"], "uinput")

    async def test_nothing_running_is_not_running(self):
        with self.assertRaises(m.ProtocolError) as cm:
            await Host(running=False)._cmd_media_key({"key": "play_pause"})
        self.assertEqual(cm.exception.code, m.NOT_RUNNING)

    async def test_uinput_failure_is_unsupported(self):
        h = Host()
        h.fail_uinput = True
        with self.assertRaises(m.ProtocolError) as cm:
            await h._cmd_media_key({"key": "play"})
        self.assertEqual(cm.exception.code, m.UNSUPPORTED)

    async def test_bad_key(self):
        for data in ({}, {"key": "volume_up"}):
            with self.assertRaises(m.ProtocolError) as cm:
                await Host()._cmd_media_key(data)
            self.assertEqual(cm.exception.code, m.BAD_REQUEST)


class PlaybackStatusTests(unittest.IsolatedAsyncioTestCase):
    def host(self, players, running=True):
        h = Host(players, running)
        h.app_state = {"running": running, "playback": "unknown"}
        h.pushed = []
        h._set_app_state = lambda **c: (h.app_state.update(c), h.pushed.append(c))
        return h

    def test_summary_prefers_playing_then_paused_then_stopped(self):
        for players, expected in (({"org.mpris.MediaPlayer2.a": "Playing", "org.mpris.MediaPlayer2.b": "Paused"}, "playing"),
                                  ({"org.mpris.MediaPlayer2.a": "Paused", "org.mpris.MediaPlayer2.b": "Stopped"}, "paused"),
                                  ({"org.mpris.MediaPlayer2.a": "Stopped"}, "stopped"), ({}, "unknown")):
            self.assertEqual(self.host(players)._playback_status(), expected, players)

    async def test_status_changes_are_pushed_once(self):
        h = self.host({"org.mpris.MediaPlayer2.a": "Paused"})
        await h._refresh_playback()
        await h._refresh_playback()
        self.assertEqual(h.pushed, [{"playback": "paused"}])
        h.players["org.mpris.MediaPlayer2.a"] = "Playing"
        await h._refresh_playback()
        self.assertEqual(h.pushed[-1], {"playback": "playing"})

    async def test_no_running_app_means_unknown_even_if_a_player_lingers(self):
        h = self.host({"org.mpris.MediaPlayer2.a": "Playing"}, running=False)
        h.app_state["playback"] = "playing"
        await h._refresh_playback()
        self.assertEqual(h.pushed, [{"playback": "unknown"}])

    async def test_a_media_key_refreshes_the_status_right_away(self):
        h = self.host({"org.mpris.MediaPlayer2.a": "Playing"})
        original = h._busctl

        def busctl(*args):
            if args[0] == "call":
                h.players["org.mpris.MediaPlayer2.a"] = "Paused"  # the player reacts to the key
            return original(*args)

        h._busctl = busctl
        await h._cmd_media_key({"key": "pause"})
        self.assertEqual(h.pushed, [{"playback": "paused"}])

    async def test_a_failing_poll_does_not_end_the_loop(self):
        h = self.host({})
        calls = []

        async def boom():
            calls.append(1)
            raise RuntimeError("bus gone")

        h._refresh_playback = boom
        from unittest import mock
        async def fake_sleep(s):
            if len(calls) >= 2:
                raise asyncio.CancelledError
        with mock.patch("decky_ha.media_keys.asyncio.sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await h._playback_loop()
        self.assertEqual(len(calls), 2)
