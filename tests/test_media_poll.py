import base64
import os
import tempfile
import unittest
from unittest import mock

from decky_ha import artwork
from decky_ha.media import MediaMixin
from decky_ha.media_keys import MediaKeyMixin
from decky_ha.proto import messages as m

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 100
JELLYFIN = "org.mpris.MediaPlayer2.JellyfinDesktop.profile_abc"
BROWSER = "org.mpris.MediaPlayer2.chromium.instance3"


def metadata(title="Ep 1", art=None, length=600_000_000, trackid="/t/1", artist="Show", album="Season 2"):
    art = art or "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()
    return (f'a{{sv}} 6 "mpris:artUrl" s "{art}" "mpris:length" x {length} "mpris:trackid" o "{trackid}" '
            f'"xesam:album" s "{album}" "xesam:artist" as 1 "{artist}" "xesam:title" s "{title}"')


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class Host(MediaMixin, MediaKeyMixin):
    def __init__(self, steam_root):
        self.players = {}   # bus name -> {"status", "metadata", "position"}
        self.app_state = {"running": False, "appid": "", "name": "", "playback": "unknown", "screen": "steam"}
        self.session = "game"
        self.offered = [{"appid": "3608369380", "name": "Twitch", "kind": "shortcut"}, {"appid": "620", "name": "Portal 2", "kind": "steam"}]
        self.pushed, self.list_fails, self.steam = [], False, steam_root
        self._init_media()

    def _load_settings(self):
        return {"apps": self.offered}

    def _steam_root(self):
        return self.steam

    def _set_app_state(self, **changes):
        self.app_state.update(changes)
        self.app_state["running"] = bool(self.app_state["appid"])

    def _push(self, name, data):
        self.pushed.append((name, data))

    def run(self, appid, name):
        self._set_app_state(appid=appid, name=name)

    def _busctl(self, *args):
        if args[0] == "list":
            if self.list_fails:
                raise RuntimeError("bus gone")
            return "\n".join(f"{n} 1 x deck :1.1 u - -" for n in ["org.freedesktop.DBus", *self.players])
        player, prop = self.players[args[1]], args[4]
        if prop == "PlaybackStatus":
            return f's "{player["status"]}"'
        if prop == "Metadata":
            return player["metadata"]
        if prop == "Position":
            return f'x {int(player["position"] * 1_000_000)}'
        raise RuntimeError(prop)

    @property
    def media(self):
        return [data for name, data in self.pushed if name == "media"]


class MediaPollTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        patch = mock.patch("decky_ha.media.time.time", self.clock)
        patch.start()
        self.addCleanup(patch.stop)
        self.h = Host(self.tmp.name)

    def jellyfin(self, status="Playing", position=10.0, **meta):
        self.h.players[JELLYFIN] = {"status": status, "metadata": metadata(**meta), "position": position}

    async def poll(self, seconds=2.0):
        self.clock.now += seconds
        await self.h._refresh_playback()

    async def test_a_playing_player_is_pushed_once_with_art(self):
        self.h.run("3608369380", "Twitch")
        self.jellyfin()
        await self.poll()
        (media,) = self.h.media
        self.assertEqual((media["title"], media["artist"], media["album"], media["kind"], media["source"]),
                         ("Ep 1", "Show", "Season 2", "video", "mpris"))
        self.assertEqual((media["duration"], media["position"]), (600.0, 10.0))
        self.assertEqual(media["art"], artwork.make_art(JPEG).id)
        self.assertEqual(self.h.art_store.chunk(media["art"])[2], JPEG)
        self.assertNotIn("base64", repr(self.h.pushed))  # image bytes never ride on a push

    async def test_normal_playback_does_not_push_again_but_a_seek_does(self):
        self.jellyfin()
        await self.poll()
        for _ in range(4):  # the position advances with the clock
            self.h.players[JELLYFIN]["position"] += 2.0
            await self.poll()
        self.assertEqual(len(self.h.media), 1)
        self.h.players[JELLYFIN]["position"] = 300.0  # the user seeks
        await self.poll()
        self.assertEqual(len(self.h.media), 2)
        self.assertEqual(self.h.media[-1]["position"], 300.0)

    async def test_pausing_pushes_the_frozen_position(self):
        self.jellyfin(position=10.0)
        await self.poll()
        self.h.players[JELLYFIN].update(status="Paused", position=12.0)
        await self.poll()
        self.assertEqual(len(self.h.media), 2)
        self.assertEqual(self.h.media[-1]["position"], 12.0)
        await self.poll()
        self.assertEqual(len(self.h.media), 2)  # still paused at the same place: nothing new

    async def test_the_art_is_loaded_once_per_track(self):
        self.jellyfin()
        with mock.patch("decky_ha.media.artwork.load_from_url", wraps=artwork.load_from_url) as load:
            await self.poll()
            await self.poll()
            await self.poll()
            self.assertEqual(load.call_count, 1)
            self.jellyfin(title="Ep 2", trackid="/t/2", art="data:image/png;base64," + base64.b64encode(PNG).decode())
            await self.poll()
            self.assertEqual(load.call_count, 2)
        self.assertEqual(self.h.media[-1]["art"], artwork.make_art(PNG).id)

    async def test_art_that_cannot_be_loaded_still_gives_metadata(self):
        self.jellyfin(art="file:///nonexistent/art.png")
        with mock.patch("decky_ha.media.artwork.load_from_url", wraps=artwork.load_from_url) as load:
            for _ in range(6):
                await self.poll()
            self.assertEqual(load.call_count, 3)  # a few tries, then it gives up for this track
        self.assertEqual(self.h.media[0]["title"], "Ep 1")
        self.assertNotIn("art", self.h.media[0])

    async def test_quitting_clears_the_group(self):
        self.h.run("3608369380", "Twitch")
        self.jellyfin()
        await self.poll()
        del self.h.players[JELLYFIN]
        self.h.run("", "")
        await self.poll()
        self.assertEqual(self.h.media[-1], {})
        self.assertIsNone(self.h.art_store.art)
        await self.poll()
        self.assertEqual(len(self.h.media), 2)

    async def test_nothing_at_all_pushes_nothing(self):
        await self.poll()
        self.assertEqual(self.h.pushed, [])

    async def test_a_stopped_player_is_no_media(self):
        self.jellyfin(status="Stopped")
        await self.poll()
        self.assertEqual(self.h.media, [])

    async def test_a_game_with_a_paused_background_player_shows_the_game_and_its_steam_art(self):
        os.makedirs(os.path.join(self.tmp.name, "appcache", "librarycache", "620"))
        with open(os.path.join(self.tmp.name, "appcache", "librarycache", "620", "library_600x900.jpg"), "wb") as f:
            f.write(JPEG)
        self.h.run("620", "Portal 2")
        self.h.players[BROWSER] = {"status": "Paused", "metadata": metadata(title="A tab"), "position": 1.0}
        await self.poll()
        (media,) = self.h.media
        self.assertEqual((media["title"], media["kind"], media["source"], media["art"]),
                         ("Portal 2", "app", "steam", artwork.make_art(JPEG).id))

    async def test_a_playing_player_wins_over_a_game(self):
        self.h.run("620", "Portal 2")
        self.h.players[BROWSER] = {"status": "Playing", "metadata": metadata(title="A video"), "position": 1.0}
        await self.poll()
        self.assertEqual(self.h.media[0]["title"], "A video")

    async def test_a_shortcut_without_a_player_gets_its_name_and_steam_art(self):
        os.makedirs(os.path.join(self.tmp.name, "userdata", "1", "config", "grid"))
        with open(os.path.join(self.tmp.name, "userdata", "1", "config", "grid", "3608369380p.png"), "wb") as f:
            f.write(PNG)
        self.h.run("3608369380", "Twitch")
        await self.poll()
        self.assertEqual(self.h.media[0]["art"], artwork.make_art(PNG).id)
        self.assertEqual(self.h.media[0]["title"], "Twitch")

    async def test_desktop_mode_shows_a_playing_player_and_reports_playback(self):
        self.h.session = "desktop"
        self.jellyfin()
        await self.poll()
        self.assertEqual(self.h.media[0]["title"], "Ep 1")
        self.assertEqual(self.h.app_state["playback"], "playing")
        self.h.players[JELLYFIN]["status"] = "Paused"
        await self.poll()
        self.assertEqual(self.h.app_state["playback"], "paused")

    async def test_one_failed_poll_keeps_the_group_two_clear_it(self):
        self.jellyfin()
        await self.poll()
        self.h.list_fails = True
        await self.poll()
        self.assertEqual(len(self.h.media), 1)
        await self.poll()
        self.assertEqual(self.h.media[-1], {})
        self.h.list_fails = False
        await self.poll()
        self.assertEqual(self.h.media[-1]["title"], "Ep 1")

    async def test_a_player_that_vanishes_between_the_list_and_the_read_counts_as_a_failed_poll(self):
        self.jellyfin()
        await self.poll()
        original = self.h._busctl

        def busctl(*args):
            if args[0] == "get-property" and args[4] == "Metadata":
                raise RuntimeError("gone")
            return original(*args)

        self.h._busctl = busctl
        await self.poll()
        self.assertEqual(len(self.h.media), 1)

    async def test_media_keys_are_unaffected_by_the_media_read(self):
        self.jellyfin()
        self.assertEqual(self.h._playback_status(), "playing")


class GetArtworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = Host(tempfile.gettempdir())
        self.data = b"\xff\xd8\xff" + os.urandom(m.ARTWORK_CHUNK_BYTES * 2 + 500)
        self.art = artwork.make_art(self.data)
        self.h.art_store.set(self.art)

    async def test_chunks_reassemble_and_each_result_fits_a_frame(self):
        got, offset, done = b"", 0, False
        while not done:
            result = await self.h._cmd_get_artwork({"id": self.art.id, "offset": offset})
            self.assertLess(len(m.encode(m.result_ok(1, result)).encode()), m.MAX_FRAME_BYTES)
            self.assertEqual((result["content_type"], result["size"], result["offset"]), ("image/jpeg", len(self.data), offset))
            piece = base64.b64decode(result["data"])
            got, offset, done = got + piece, offset + len(piece), result["done"]
        self.assertEqual(got, self.data)
        self.assertEqual(artwork.make_art(got).id, self.art.id)

    async def test_default_offset_is_zero(self):
        self.assertEqual((await self.h._cmd_get_artwork({"id": self.art.id}))["offset"], 0)

    async def test_an_old_id_is_not_running(self):
        with self.assertRaises(m.ProtocolError) as cm:
            await self.h._cmd_get_artwork({"id": "0123456789abcdef"})
        self.assertEqual(cm.exception.code, m.NOT_RUNNING)
        self.h.art_store.set(None)
        with self.assertRaises(m.ProtocolError) as cm:
            await self.h._cmd_get_artwork({"id": self.art.id})
        self.assertEqual(cm.exception.code, m.NOT_RUNNING)

    async def test_bad_requests(self):
        for data in ({}, {"id": 5}, {"id": self.art.id, "offset": -1}, {"id": self.art.id, "offset": len(self.data)},
                     {"id": self.art.id, "offset": "0"}):
            with self.assertRaises(m.ProtocolError, msg=repr(data)) as cm:
                await self.h._cmd_get_artwork(data)
            self.assertEqual(cm.exception.code, m.BAD_REQUEST)

    async def test_it_is_a_registered_command(self):
        import main
        self.assertIn("get_artwork", main.Plugin._commands(main.Plugin.__new__(main.Plugin)))


if __name__ == "__main__":
    unittest.main()
