import asyncio
import unittest

from decky_ha.media_keys import KEY_CODES, MPRIS_METHODS, MediaKeyMixin
from decky_ha.proto import messages as m


class Host(MediaKeyMixin):
    def __init__(self, players=None, running=True):
        self.players = players or {}
        self.app_state = {"running": running}
        self.calls, self.keys, self.fail_uinput = [], [], False

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
        self.assertEqual(h.calls[-1], ("call", "org.mpris.MediaPlayer2.jellyfin", "/org/mpris/MediaPlayer2",
                                       "org.mpris.MediaPlayer2.Player", "PlayPause"))
        self.assertEqual(h.keys, [])

    async def test_every_key_maps_to_a_method_and_a_key_code(self):
        self.assertEqual(set(MPRIS_METHODS), set(KEY_CODES))
        for key, method in MPRIS_METHODS.items():
            h = Host({"org.mpris.MediaPlayer2.p": "Playing"})
            await h._cmd_media_key({"key": key})
            self.assertEqual(h.calls[-1][-1], method)

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
