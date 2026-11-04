import asyncio
import os
import unittest

from tests.sendspin_peer import FakeServer  # noqa: F401
from tests.test_sendspin_session import Harness, SessionTestCase

ROLES = ("player@v1", "metadata@v1", "artwork@v1", "controller@v1")
JPEG = b"\xff\xd8\xff\xe0" + os.urandom(150000)
COMMANDS = ["play", "pause", "stop", "next", "previous", "repeat_off", "shuffle", "volume", "mute"]


def meta(title="404", speed=1000, progress=39, timestamp=0):
    return {"timestamp": timestamp, "title": title, "artist": "Knife Party", "album": "Abandon Ship",
            "progress": {"track_progress": progress, "track_duration": 297000, "playback_speed": speed}}


class CardHarness(Harness):
    def __init__(self, **kw):
        self.cards = []
        super().__init__(on_card=self.cards.append, **kw)


class CardTests(SessionTestCase):
    async def start(self, roles=ROLES, **kw):
        h = CardHarness(**kw)
        h = await h.start()
        self.addAsyncCleanup(h.stop)
        await h.connect(roles=roles)
        await h.wait_for(lambda: h.sessions and h.sessions[0].clock.converged)
        return h

    async def test_the_card_follows_metadata_cover_and_controller(self):
        h = await self.start()
        s = h.sessions[0]
        await h.peer.send("server/state", {"controller": {"supported_commands": COMMANDS, "volume": 100, "muted": False}})
        await h.peer.send("server/state", {"metadata": meta(timestamp=h.peer.server_now())})
        await h.peer.send_cover(JPEG, timestamp_us=h.peer.server_now() - 1000)
        await h.wait_for(lambda: s.card.snapshot(None, 0)["art"] is not None)
        await h.wait_for(lambda: h.cards and h.cards[-1]["art"] and h.cards[-1]["active"])
        card = h.cards[-1]
        self.assertEqual((card["title"], card["artist"], card["album"], card["playing"]), ("404", "Knife Party", "Abandon Ship", True))
        self.assertEqual(card["commands"], ["previous", "play", "pause", "next"])
        self.assertEqual(s.artwork.lookup(card["art"]).data, JPEG)
        self.assertEqual((card["volume"], card["muted"]), (50, False))

    async def test_pause_metadata_updates_the_card(self):
        h = await self.start()
        await h.peer.send("server/state", {"metadata": meta(timestamp=h.peer.server_now())})
        await h.wait_for(lambda: h.cards and h.cards[-1]["playing"])
        await h.peer.send("server/state", {"metadata": meta(speed=0, progress=5000, timestamp=h.peer.server_now())})
        await h.wait_for(lambda: not h.cards[-1]["playing"])
        self.assertEqual(h.cards[-1]["position_ms"], 5000)
        await h.peer.send("server/state", {"metadata": meta(title="Next one", timestamp=h.peer.server_now())})
        await h.wait_for(lambda: h.cards[-1]["title"] == "Next one")

    async def test_send_command_sends_one_frame_and_refuses_what_is_not_offered(self):
        h = await self.start()
        s = h.sessions[0]
        await h.peer.send("server/state", {"metadata": meta(timestamp=h.peer.server_now()),
                                           "controller": {"supported_commands": ["play", "pause", "previous"]}})
        await h.wait_for(lambda: s.card_commands == ["previous", "play", "pause"])
        self.assertFalse(await s.send_command("next"))
        self.assertFalse(await s.send_command("stop"))
        self.assertFalse(await s.send_command("nonsense"))
        self.assertTrue(await s.send_command("pause"))
        message = await h.peer.next("client/command")
        self.assertEqual(message[1], {"controller": {"command": "pause"}})
        await h.peer.send("server/state", {"controller": {"supported_commands": ["next"]}})
        await h.wait_for(lambda: s.card_commands == ["next"])
        self.assertTrue(await s.send_command("next"))
        self.assertEqual((await h.peer.next("client/command"))[1], {"controller": {"command": "next"}})
        with self.assertRaises(asyncio.TimeoutError):
            await h.peer.next("client/command", timeout=0.2)  # exactly one frame each

    async def test_set_player_volume_changes_the_sink_saves_and_reports_it(self):
        h = await self.start()
        s = h.sessions[0]
        await s.set_player_volume(30)
        await h.wait_for(lambda: h.sink.volume == 30)
        self.assertIn({"volume": 30}, h.settings)
        state = None
        while state is None or state["player"]["volume"] != 30:
            _, state = await h.peer.next("client/state")
        self.assertEqual(state["player"]["volume"], 30)
        await h.wait_for(lambda: h.cards and h.cards[-1]["volume"] == 30)
        await s.set_player_volume(250)
        self.assertEqual(s.volume, 100)
        await s.set_player_volume(-4)
        self.assertEqual(s.volume, 0)

    async def test_a_server_volume_command_updates_the_card_too(self):
        h = await self.start()
        await h.peer.send("server/command", {"player": {"command": "volume", "volume": 20}})
        await h.wait_for(lambda: h.cards and h.cards[-1]["volume"] == 20)
        await h.peer.send("server/command", {"player": {"command": "mute", "mute": True}})
        await h.wait_for(lambda: h.cards[-1]["muted"])

    async def test_a_broken_cover_is_discarded_without_disconnecting_and_a_new_one_changes_the_id(self):
        h = await self.start()
        s = h.sessions[0]
        await h.peer.send("server/state", {"metadata": meta(timestamp=h.peer.server_now())})
        await h.peer.send_cover(JPEG, timestamp_us=0)
        await h.wait_for(lambda: s.artwork.current() is not None)
        first = s.artwork.current().id
        await h.peer.send_plain(bytes([8, 0]) + b"orphan part")
        await h.wait_for(lambda: s.artwork.discarded == 1)
        self.assertEqual(s.artwork.current().id, first)
        self.assertIsNone(s.ended)
        await h.peer.send_cover(JPEG[:5000], timestamp_us=0)
        await h.wait_for(lambda: s.artwork.current().id != first)
        await h.wait_for(lambda: h.cards[-1]["art"] == s.artwork.current().id)

    async def test_a_cover_scheduled_for_later_waits_for_the_playout_loop(self):
        h = await self.start()
        s = h.sessions[0]
        await h.peer.send_cover(JPEG[:3000], timestamp_us=h.peer.server_now() + 400_000)
        await h.wait_for(lambda: s.artwork.waiting())
        self.assertIsNone(s.artwork.current())
        await h.wait_for(lambda: s.artwork.current() is not None, timeout=3)

    async def test_roles_the_server_did_not_activate_leave_the_player_working(self):
        h = await self.start(roles=("player@v1",))
        s = h.sessions[0]
        await h.peer.send("server/state", {"metadata": meta(), "controller": {"supported_commands": COMMANDS}})
        await h.peer.send_cover(JPEG[:2000])
        await h.peer.send("server/command", {"player": {"command": "volume", "volume": 12}})
        await h.wait_for(lambda: s.volume == 12)
        self.assertFalse(s.card.snapshot(None, 0)["active"])
        self.assertEqual((s.card_commands, s.artwork.current()), ([], None))
        self.assertFalse(await s.send_command("play"))
        self.assertIsNone(s.ended)

    async def test_losing_the_metadata_role_clears_the_track(self):
        h = await self.start()
        s = h.sessions[0]
        await h.peer.send("server/state", {"metadata": meta(timestamp=h.peer.server_now())})
        await h.wait_for(lambda: h.cards and h.cards[-1]["active"])
        await h.peer.send("server/activate", {"activities": ["playback"], "active_roles": ["player@v1"]})
        await h.wait_for(lambda: not h.cards[-1]["active"])
        self.assertEqual(s.ended, None)

    async def test_card_pushes_are_coalesced_to_four_a_second(self):
        h = await self.start()
        for i in range(30):
            await h.peer.send("server/state", {"metadata": meta(title=f"T{i}", timestamp=h.peer.server_now())})
        await h.wait_for(lambda: h.cards and h.cards[-1]["title"] == "T29", timeout=3)
        self.assertLess(len(h.cards), 12)

    async def test_the_session_end_emits_an_inactive_card(self):
        h = await self.start()
        await h.peer.send("server/state", {"metadata": meta(timestamp=h.peer.server_now())})
        await h.wait_for(lambda: h.cards and h.cards[-1]["active"])
        h.sessions[0].request_stop("shutdown")
        await asyncio.wait_for(h.finished.wait(), 5)
        self.assertFalse(h.cards[-1]["active"])
        self.assertEqual(h.cards[-1]["title"], "")


if __name__ == "__main__":
    unittest.main()
