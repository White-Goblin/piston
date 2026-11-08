import asyncio
import unittest

from piston_device.media_keys import KEY_CODES, MPRIS_METHODS, MediaKeyMixin
from piston_device.proto import messages as m


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


class SeekHost(Host):
    """A host whose players report a position, a track id and a length, and can refuse SetPosition."""
    TRACK = "/org/chromium/MediaPlayer2/TrackList/Track1"

    def __init__(self, *args, position=100.0, trackid=True, length=600.0, set_position_fails=False, **kw):
        super().__init__(*args, **kw)
        self.position, self.trackid, self.length, self.set_position_fails = position, trackid, length, set_position_fails

    def _busctl(self, *args):
        if args[0] == "get-property" and args[-1] == "Metadata":
            self.calls.append(args)
            tid = f'"mpris:trackid" o "{self.TRACK}" ' if self.trackid else ""
            return f'a{{sv}} 2 {tid}"mpris:length" x {round(self.length * 1_000_000)}'
        if args[0] == "get-property" and args[-1] == "Position":
            self.calls.append(args)
            return f"x {round(self.position * 1_000_000)}"
        if args[0] == "call" and args[4] == "SetPosition" and self.set_position_fails:
            self.calls.append(args)
            raise RuntimeError("rejected")
        return super()._busctl(*args)


class SeekTests(unittest.IsolatedAsyncioTestCase):
    P = {"org.mpris.MediaPlayer2.p": "Paused"}

    def moves(self, h):
        return [c[4:] for c in h.calls if c[0] == "call" and c[4] in ("Seek", "SetPosition")]

    async def test_a_position_goes_to_set_position_with_the_track_id(self):
        h = SeekHost(self.P)
        self.assertEqual(await h._cmd_media_seek({"position": 213}), {"via": "mpris", "player": "p"})
        self.assertEqual(self.moves(h), [("SetPosition", "ox", SeekHost.TRACK, "213000000")])

    async def test_an_offset_is_added_to_the_live_position(self):
        h = SeekHost(self.P, position=100.0)
        await h._cmd_media_seek({"offset": 10})
        await h._cmd_media_seek({"offset": -30})
        self.assertEqual([m_[3] for m_ in self.moves(h)], ["110000000", "70000000"])

    async def test_the_target_stays_inside_the_track(self):
        h = SeekHost(self.P, position=595.0, length=600.0)
        await h._cmd_media_seek({"offset": 10})
        await h._cmd_media_seek({"position": 0})
        h2 = SeekHost(self.P, position=5.0)
        await h2._cmd_media_seek({"offset": -30})
        self.assertEqual([m_[3] for m_ in self.moves(h)], ["600000000", "0"])
        self.assertEqual([m_[3] for m_ in self.moves(h2)], ["0"])

    async def test_without_a_track_id_or_when_rejected_it_falls_back_to_a_relative_seek(self):
        for kw in ({"trackid": False}, {"set_position_fails": True}):
            h = SeekHost(self.P, position=100.0, **kw)
            await h._cmd_media_seek({"position": 130})
            await h._cmd_media_seek({"offset": -10})
            self.assertEqual([m_[0:3] for m_ in self.moves(h) if m_[0] == "Seek"], [("Seek", "x", "--"), ("Seek", "x", "--")], kw)
            self.assertEqual([m_[3] for m_ in self.moves(h) if m_[0] == "Seek"], ["30000000", "-10000000"], kw)

    async def test_a_seek_does_not_press_play_or_a_key(self):
        h = SeekHost({"org.mpris.MediaPlayer2.p": "Playing"})
        await h._cmd_media_seek({"position": 50})
        self.assertEqual([c[4] for c in h.calls if c[0] == "call"], ["SetPosition"])
        self.assertEqual(h.keys, [])

    async def test_a_player_that_rejects_everything_falls_through_to_the_next(self):
        h = SeekHost({"org.mpris.MediaPlayer2.a": "broken", "org.mpris.MediaPlayer2.b": "Paused"})
        self.assertEqual((await h._cmd_media_seek({"offset": 5}))["player"], "b")

    async def test_no_player_accepts_it_is_unsupported_and_there_is_no_virtual_key(self):
        for players in ({"org.mpris.MediaPlayer2.a": "broken"}, {}):
            h = SeekHost(players)
            with self.assertRaises(m.ProtocolError) as cm:
                await h._cmd_media_seek({"offset": 5})
            self.assertEqual(cm.exception.code, m.UNSUPPORTED)
            self.assertEqual(h.keys, [])

    async def test_nothing_running_is_not_running(self):
        with self.assertRaises(m.ProtocolError) as cm:
            await SeekHost(running=False)._cmd_media_seek({"offset": 5})
        self.assertEqual(cm.exception.code, m.NOT_RUNNING)

    async def test_bad_requests(self):
        for data in ({}, {"offset": 1, "position": 1}, {"offset": "5"}, {"offset": True}, {"offset": None}, {"offset": float("nan")},
                     {"offset": float("inf")}, {"offset": 3601}, {"offset": -3601}, {"position": -1}, {"position": "5"},
                     {"position": float("nan")}, {"position": 7 * 24 * 3600 + 1}):
            with self.assertRaises(m.ProtocolError, msg=str(data)) as cm:
                await SeekHost(self.P)._cmd_media_seek(data)
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

    async def test_without_a_running_app_the_status_comes_from_a_playing_or_paused_player(self):
        h = self.host({"org.mpris.MediaPlayer2.a": "Playing"}, running=False)
        await h._refresh_playback()
        self.assertEqual(h.pushed, [{"playback": "playing"}])
        h.players["org.mpris.MediaPlayer2.a"] = "Paused"
        await h._refresh_playback()
        self.assertEqual(h.pushed[-1], {"playback": "paused"})

    async def test_without_a_running_app_and_without_a_live_player_it_is_unknown(self):
        h = self.host({"org.mpris.MediaPlayer2.a": "Stopped"}, running=False)
        h.app_state["playback"] = "playing"
        await h._refresh_playback()
        self.assertEqual(h.pushed, [{"playback": "unknown"}])
        h2 = self.host({}, running=False)
        h2.app_state["playback"] = "paused"
        await h2._refresh_playback()
        self.assertEqual(h2.pushed, [{"playback": "unknown"}])

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
        with mock.patch("piston_device.media_keys.asyncio.sleep", fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await h._playback_loop()
        self.assertEqual(len(calls), 2)
