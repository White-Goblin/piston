import unittest

from . import fake  # noqa: F401
from piston_device.sendspin.card import CardState, changed

META = {"timestamp": 1_000_000, "title": "404", "artist": "Knife Party", "album": "Abandon Ship",
        "progress": {"track_progress": 10_000, "track_duration": 297_000, "playback_speed": 1000}}


def card(meta=META, commands=("play", "pause", "next", "previous", "stop", "shuffle"), session=True):
    c = CardState()
    c.set_session(session)
    if meta is not None:
        c.apply_metadata(meta)
    c.apply_controller({"supported_commands": list(commands)})
    return c


class SnapshotTests(unittest.TestCase):
    def test_playing_position_is_extrapolated_from_the_metadata_timestamp(self):
        s = card().snapshot(1_000_000 + 5_000_000, 100.0)   # five seconds later
        self.assertEqual((s["active"], s["playing"], s["position_ms"], s["duration_ms"]), (True, True, 15_000, 297_000))
        self.assertEqual((s["title"], s["artist"], s["album"], s["speed"], s["updated_at"]), ("404", "Knife Party", "Abandon Ship", 1.0, 100.0))

    def test_paused_position_does_not_move(self):
        meta = {**META, "progress": {**META["progress"], "playback_speed": 0}}
        s = card(meta).snapshot(1_000_000 + 60_000_000, 1.0)
        self.assertEqual((s["playing"], s["position_ms"]), (False, 10_000))

    def test_a_sped_up_track_moves_faster(self):
        meta = {**META, "progress": {**META["progress"], "playback_speed": 2000}}
        s = card(meta).snapshot(1_000_000 + 5_000_000, 1.0)
        self.assertEqual((s["position_ms"], s["speed"]), (20_000, 2.0))

    def test_an_unknown_duration_is_none_and_the_position_still_moves(self):
        meta = {**META, "progress": {"track_progress": 1000, "track_duration": 0, "playback_speed": 1000}}
        s = card(meta).snapshot(1_000_000 + 3_000_000, 1.0)
        self.assertEqual((s["duration_ms"], s["position_ms"]), (None, 4000))

    def test_the_position_is_clamped_to_the_track(self):
        s = card().snapshot(1_000_000 + 9_999_000_000, 1.0)
        self.assertEqual(s["position_ms"], 297_000)
        self.assertEqual(card().snapshot(1_000_000 - 99_000_000, 1.0)["position_ms"], 0)

    def test_without_progress_there_is_no_position_and_a_new_metadata_without_it_clears_it(self):
        c = card()
        c.apply_metadata({"timestamp": 5, "title": "x"})
        s = c.snapshot(10, 1.0)
        self.assertEqual((s["position_ms"], s["duration_ms"], s["playing"], s["speed"]), (None, None, False, 0.0))

    def test_an_unusable_clock_reports_the_position_as_sent(self):
        self.assertEqual(card().snapshot(None, 1.0)["position_ms"], 10_000)

    def test_active_needs_a_title_or_artist_and_a_session(self):
        self.assertTrue(card({"timestamp": 1, "artist": "A"}).snapshot(1, 1.0)["active"])
        self.assertFalse(card({"timestamp": 1}).snapshot(1, 1.0)["active"])
        self.assertFalse(card({"timestamp": 1, "title": None, "artist": ""}).snapshot(1, 1.0)["active"])
        self.assertFalse(card(session=False).snapshot(1, 1.0)["active"])
        self.assertFalse(card(None).snapshot(1, 1.0)["active"])

    def test_clear_forgets_everything(self):
        c = card()
        c.set_cover("7-abc")
        c.set_volume(40, True)
        c.clear()
        s = c.snapshot(1, 1.0)
        self.assertEqual((s["active"], s["title"], s["art"], s["commands"], s["volume"], s["muted"], s["position_ms"]), (False, "", None, [], 0, False, None))

    def test_commands_are_the_offered_card_subset_in_fixed_order(self):
        self.assertEqual(card().commands(), ["previous", "play", "pause", "next"])
        self.assertEqual(card(commands=("next", "pause", "stop", "volume", "seek")).commands(), ["pause", "next"])
        self.assertEqual(card(commands=()).commands(), [])

    def test_volume_and_cover(self):
        c = card()
        c.set_volume(35, False)
        c.set_cover("3-x")
        s = c.snapshot(1_000_000, 1.0)
        self.assertEqual((s["volume"], s["muted"], s["art"]), (35, False, "3-x"))


class ChangedTests(unittest.TestCase):
    def snap(self, c, server_us, wall):
        return c.snapshot(server_us, wall)

    def test_the_first_snapshot_is_always_a_change(self):
        self.assertTrue(changed(None, card().snapshot(1_000_000, 1.0)))

    def test_a_playing_card_does_not_change_just_because_time_passed(self):
        c = card()
        a, b = self.snap(c, 1_000_000, 10.0), self.snap(c, 3_000_000, 12.0)
        self.assertFalse(changed(a, b))

    def test_other_field_changes_are_changes(self):
        c = card()
        a = self.snap(c, 1_000_000, 10.0)
        c.set_volume(10, False)
        self.assertTrue(changed(a, self.snap(c, 1_000_000, 10.0)))
        c2 = card()
        c2.set_cover("1")
        self.assertTrue(changed(a, self.snap(c2, 1_000_000, 10.0)))
        c3 = card(commands=("play",))
        self.assertTrue(changed(a, self.snap(c3, 1_000_000, 10.0)))

    def test_a_seek_while_playing_is_a_change_and_a_paused_move_is_too(self):
        a = self.snap(card(), 1_000_000, 10.0)
        seek = {**META, "progress": {**META["progress"], "track_progress": 100_000}}
        self.assertTrue(changed(a, self.snap(card(seek), 1_000_000, 10.5)))
        paused = {**META, "progress": {**META["progress"], "playback_speed": 0}}
        p1 = self.snap(card(paused), 1_000_000, 10.0)
        p2 = self.snap(card({**paused, "progress": {**paused["progress"], "track_progress": 20_000}}), 1_000_000, 11.0)
        self.assertTrue(changed(p1, p2))
        self.assertFalse(changed(p1, self.snap(card(paused), 9_000_000, 20.0)))


if __name__ == "__main__":
    unittest.main()
