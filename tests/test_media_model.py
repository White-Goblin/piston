import unittest

from decky_ha import media_model as mm

GAME = {"appid": "620", "name": "Portal 2", "game": True}
SHORTCUT = {"appid": "3608369380", "name": "Twitch", "game": False}


def player(status, **meta):
    return {"name": "org.mpris.MediaPlayer2.x", "status": status, "meta": meta, "position": meta.pop("position", None)}


class AppInfoTests(unittest.TestCase):
    def test_kinds(self):
        offered = [{"appid": "620", "kind": "steam"}, {"appid": "3608369380", "kind": "shortcut"}]
        self.assertEqual(mm.app_info({"appid": "3608369380", "name": "Twitch"}, offered),
                         {"appid": "3608369380", "name": "Twitch", "game": False})
        self.assertTrue(mm.app_info({"appid": "620", "name": "Portal 2"}, offered)["game"])
        self.assertFalse(mm.app_info({"appid": "2451505133", "name": "x"}, [])["game"])  # not offered, shortcut-range id
        self.assertTrue(mm.app_info({"appid": "730", "name": "x"}, [])["game"])
        self.assertIsNone(mm.app_info({"appid": "", "name": ""}, offered))


class ChoosePlayerTests(unittest.TestCase):
    def test_playing_wins_over_paused(self):
        paused, playing = player("Paused", title="a"), player("Playing", title="b")
        self.assertIs(mm.choose_player([paused, playing], SHORTCUT), playing)
        self.assertIs(mm.choose_player([paused, playing], GAME), playing)

    def test_paused_is_ignored_while_a_game_runs(self):
        self.assertIsNone(mm.choose_player([player("Paused", title="tab")], GAME))

    def test_paused_is_used_for_a_shortcut_or_no_app(self):
        paused = player("Paused", title="a")
        self.assertIs(mm.choose_player([paused], SHORTCUT), paused)
        self.assertIs(mm.choose_player([paused], None), paused)  # desktop mode

    def test_stopped_and_unknown_status_count_as_nothing(self):
        self.assertIsNone(mm.choose_player([player("Stopped", title="a"), player("", title="b")], SHORTCUT))
        self.assertIsNone(mm.choose_player([], None))


class BuildMediaTests(unittest.TestCase):
    def test_full_mpris_media(self):
        p = {"status": "Playing", "position": 12.5,
             "meta": {"title": "Ep", "artist": "Show", "album": "Season 2", "length": 600.0}}
        self.assertEqual(mm.build_media(p, SHORTCUT, "abc", now=1000.0),
                         {"kind": "video", "source": "mpris", "title": "Ep", "artist": "Show", "album": "Season 2",
                          "duration": 600.0, "position": 12.5, "position_at": 1000.0, "art": "abc"})

    def test_artist_and_album_do_not_change_the_kind(self):
        p = {"status": "Playing", "position": None, "meta": {"title": "Ep", "artist": "Show", "album": "Season 2"}}
        self.assertEqual(mm.build_media(p, None)["kind"], "video")

    def test_no_length_means_no_progress(self):
        p = {"status": "Playing", "position": 5.0, "meta": {"title": "Live"}}
        media = mm.build_media(p, SHORTCUT)
        self.assertNotIn("duration", media)
        self.assertNotIn("position", media)

    def test_position_is_clamped_to_the_length(self):
        p = {"status": "Playing", "position": 700.0, "meta": {"title": "T", "length": 600.0}}
        self.assertEqual(mm.build_media(p, None, now=1.0)["position"], 600.0)

    def test_empty_title_falls_back_to_the_app_name(self):
        p = {"status": "Playing", "position": None, "meta": {"artist": "A"}}
        self.assertEqual(mm.build_media(p, SHORTCUT)["title"], "Twitch")

    def test_a_player_that_says_nothing_and_no_app_gives_nothing(self):
        self.assertEqual(mm.build_media({"status": "Playing", "meta": {}, "position": None}, None), {})

    def test_steam_source(self):
        self.assertEqual(mm.build_media(None, GAME, "art1"),
                         {"title": "Portal 2", "kind": "app", "source": "steam", "art": "art1"})
        self.assertEqual(mm.build_media(None, GAME), {"title": "Portal 2", "kind": "app", "source": "steam"})
        self.assertEqual(mm.build_media(None, None), {})


if __name__ == "__main__":
    unittest.main()
