import time
import unittest

from decky_ha import mpris_meta as mm

# Captured on the dev device (art data trimmed to a few characters).
JELLYFIN = ('a{sv} 8 "mpris:artUrl" s "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAEBAQEB" '
            '"mpris:length" x 675107000 "mpris:trackid" o "/org/mpris/MediaPlayer2/Track/5f3a" '
            '"xesam:album" s "Season 2" "xesam:artist" as 1 "Thomas & Friends: All Engines Go" '
            '"xesam:contentCreated" s "2022" "xesam:title" s "Snowplow Struttin\\\'" "xesam:trackNumber" i 4')
YOUTUBE = ('a{sv} 6 "mpris:artUrl" s "file:///run/user/1000/app/rocks.shy.VacuumTube/.org.chromium.Chromium.EvQavp" '
           '"mpris:length" x 1841241000 "mpris:trackid" o "/org/chromium/MediaPlayer2/TrackList/Track77D29" '
           '"xesam:album" s "" "xesam:artist" as 1 "Ms Rachel - Toddler Learning Videos" '
           '"xesam:title" s "Learn to Talk for Babies and Toddlers - Ms Rachel visits the Play Street Museum in Manhattan!"')


class MetadataTests(unittest.TestCase):
    def test_jellyfin_sample(self):
        meta = mm.parse_metadata(JELLYFIN)
        self.assertEqual(meta["title"], "Snowplow Struttin'")
        self.assertEqual(meta["artist"], "Thomas & Friends: All Engines Go")
        self.assertEqual(meta["album"], "Season 2")
        self.assertAlmostEqual(meta["length"], 675.107)
        self.assertTrue(meta["art_url"].startswith("data:image/jpeg;base64,"))
        self.assertTrue(meta["trackid"].endswith("/Track/5f3a"))

    def test_youtube_sample_omits_the_empty_album(self):
        meta = mm.parse_metadata(YOUTUBE)
        self.assertNotIn("album", meta)
        self.assertEqual(meta["artist"], "Ms Rachel - Toddler Learning Videos")
        self.assertEqual(meta["art_url"], "file:///run/user/1000/app/rocks.shy.VacuumTube/.org.chromium.Chromium.EvQavp")
        self.assertAlmostEqual(meta["length"], 1841.241)

    def test_missing_fields_and_zero_length_are_absent(self):
        meta = mm.parse_metadata('a{sv} 2 "mpris:length" x 0 "xesam:title" s "Live"')
        self.assertEqual(meta, {"title": "Live"})
        self.assertEqual(mm.parse_metadata('a{sv} 0'), {})

    def test_several_artists_are_joined(self):
        meta = mm.parse_metadata('a{sv} 1 "xesam:artist" as 2 "A" "B"')
        self.assertEqual(meta["artist"], "A, B")

    def test_long_title_is_capped(self):
        meta = mm.parse_metadata('a{sv} 1 "xesam:title" s "%s"' % ("x" * 400))
        self.assertEqual(len(meta["title"]), 255)

    def test_escapes(self):
        meta = mm.parse_metadata(r'a{sv} 1 "xesam:title" s "say \"hi\"\\ \xc3\xa9 \n"')
        self.assertEqual(meta["title"], 'say "hi"\\ é')

    def test_other_types_are_skipped_and_unknown_types_stop_the_scan(self):
        meta = mm.parse_metadata('a{sv} 4 "a:flag" b true "a:n" d 1.5 "xesam:title" s "T" "z:odd" a{ss} 0')
        self.assertEqual(meta, {"title": "T"})
        meta = mm.parse_metadata('a{sv} 3 "z:odd" a{ss} 1 "k" "v" "xesam:title" s "T"')
        self.assertEqual(meta, {})  # the scan cannot go past a type it cannot skip

    def test_malformed_input_returns_an_empty_dict(self):
        for raw in (None, "", 5, "garbage", 'a{sv} 2 "xesam:title" s', 'a{sv} x "xesam:title"'):
            self.assertEqual(mm.parse_metadata(raw), {}, msg=repr(raw))

    def test_a_megabyte_of_art_parses_quickly(self):
        raw = 'a{sv} 2 "mpris:artUrl" s "data:image/jpeg;base64,%s" "xesam:title" s "T"' % ("A" * 1_000_000)
        start = time.monotonic()
        meta = mm.parse_metadata(raw)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual((meta["title"], len(meta["art_url"])), ("T", 1_000_023))


class PositionTests(unittest.TestCase):
    def test_position(self):
        self.assertAlmostEqual(mm.parse_position("x 289428972\n"), 289.428972)
        for raw in (None, "", "x -5", "s \"a\"", "x abc"):
            self.assertIsNone(mm.parse_position(raw), msg=repr(raw))


if __name__ == "__main__":
    unittest.main()
