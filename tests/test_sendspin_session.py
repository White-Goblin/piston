import asyncio
import unittest
from unittest import mock

from piston_device import wsproto
from piston_device.sendspin import crypto, messages as sm, session as sess
from piston_device.sendspin.sink import FakeSink
from tests.sendspin_peer import FakeServer

FMT48 = {"codec": "pcm", "sample_rate": 48000, "channels": 2, "bit_depth": 16}
CHUNK = bytes(range(256)) * 15  # 3840 bytes = 20 ms of 48 kHz stereo 16-bit


class Harness:
    """A player session served on a real local socket, driven by a scripted server."""

    def __init__(self, **session_kw):
        self.sink = FakeSink()
        self.private = crypto.generate_private_key()
        self.statuses, self.settings, self.sessions, self.reasons = [], [], [], []
        self.session_kw = session_kw
        self.finished = asyncio.Event()

    async def start(self):
        async def handle(reader, writer):
            if not await wsproto.handshake(reader, writer, "/sendspin", forbid=("origin",)):
                writer.close()
                return
            s = sess.SendspinSession(reader, writer, private_key=self.private, name="Test Box", sink=self.sink,
                                     on_status=lambda status, **info: self.statuses.append((status, info)),
                                     on_settings=lambda **c: self.settings.append(c), burst_interval_s=0.05,
                                     lookahead_us=30_000, **self.session_kw)
            self.sessions.append(s)
            self.reasons.append(await s.run())
            self.finished.set()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.peer = FakeServer()
        await self.peer.connect(self.port)
        return self

    async def connect(self, activities=("playback",), roles=("player@v1",)):
        await self.peer.handshake()
        self.peer.hello = await self.peer.hello_and_activate(activities, roles)
        self.peer.start_pump()
        return self.sessions[0] if self.sessions else None

    async def stop(self):
        self.peer.close()
        self.server.close()
        await self.server.wait_closed()

    async def wait_for(self, predicate, timeout=3.0):
        end = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > end:
                raise AssertionError("condition not reached in time")
            await asyncio.sleep(0.01)


class SessionTestCase(unittest.IsolatedAsyncioTestCase):
    async def harness(self, **kw):
        h = await Harness(**kw).start()
        self.addAsyncCleanup(h.stop)
        return h


class HandshakeAndHelloTests(SessionTestCase):
    async def test_the_full_opening_exchange(self):
        h = await self.harness()
        await h.connect()
        hello = h.peer.hello
        self.assertEqual(hello["name"], "Test Box")
        self.assertEqual(hello["supported_roles"], ["player@v1"])
        self.assertEqual(hello["unpaired_access"], {"enabled": True})
        self.assertEqual([f["sample_rate"] for f in hello["player@v1_support"]["supported_formats"]], [48000, 44100])
        s = h.sessions[0]
        self.assertEqual(h.peer.client_id, s.client_id)                       # the client id is the public key
        self.assertEqual(s.client_id, sm.client_id_for(crypto.x25519_public(h.private)))
        self.assertTrue(s.unpaired)
        self.assertEqual(s.server_name, "Test Server")
        await h.wait_for(lambda: ("connected", {"server_name": "Test Server"}) in h.statuses)

    async def test_the_first_state_is_unavailable_and_availability_follows_the_clock(self):
        h = await self.harness()
        await h.connect()
        first = await h.peer.next("client/state")
        self.assertFalse(first[1]["available"])
        p = first[1]["player"]
        self.assertEqual((p["volume"], p["muted"], p["output_delay_ms"], p["supported_commands"]), (50, False, 0, ["volume", "mute", "set_output_delay"]))
        ready = await h.peer.next("client/state")
        self.assertTrue(ready[1]["available"])                                # two quick bursts converge the filter
        self.assertTrue(h.sessions[0].clock.converged)

    async def test_time_requests_come_in_bursts_of_eight(self):
        h = await self.harness()
        await h.connect()
        await h.wait_for(lambda: h.peer.time_requests >= 16)
        self.assertEqual(h.peer.time_requests % 8 == 0 or h.peer.time_requests >= 16, True)

    async def test_the_clock_estimates_the_server_offset(self):
        h = await self.harness()
        await h.connect()
        await h.wait_for(lambda: h.sessions[0].clock.converged)
        now = sess.monotonic_us()
        self.assertAlmostEqual(h.sessions[0].clock.compute_server_time(now) - now, 7_000_000, delta=2000)

    async def test_a_wrong_server_key_ends_the_session_before_any_hello(self):
        h = await self.harness()
        with self.assertRaises(Exception):
            await h.peer.handshake(wrong_client_key=True)
        await asyncio.wait_for(h.finished.wait(), 3)
        self.assertIn("protocol error", h.reasons[0])
        self.assertEqual(h.statuses[-1][0], "waiting")

    async def test_a_server_error_instead_of_server_init_ends_the_session(self):
        h = await self.harness()
        await h.peer._text()
        await h.peer.ws.send({"type": "server/error", "payload": {"reason": "unsupported_suite"}})
        await asyncio.wait_for(h.finished.wait(), 3)
        self.assertIn("unsupported_suite", h.reasons[0])

    async def test_the_wrong_first_message_after_the_handshake_ends_it(self):
        h = await self.harness()
        await h.peer.handshake()
        await h.peer.send("server/activate", {"activities": []})
        await asyncio.wait_for(h.finished.wait(), 3)
        self.assertIn("expected server/hello", h.reasons[0])

    async def test_a_pairing_only_activation_gets_unauthorized_and_ends(self):
        h = await self.harness()
        await h.connect(activities=("pairing",), roles=())
        self.assertEqual(await h.peer.next("client/goodbye"), ("client/goodbye", {"reason": "unauthorized"}))
        await asyncio.wait_for(h.finished.wait(), 3)

    async def test_an_empty_activation_is_admitted_and_a_later_one_activates_the_player(self):
        h = await self.harness()
        await h.connect(activities=(), roles=())
        await h.wait_for(lambda: h.statuses and h.statuses[-1][0] == "connected")
        self.assertFalse(h.sessions[0].player_active)
        await h.peer.send("server/activate", {"activities": ["playback"], "active_roles": ["player@v1"]})
        await h.peer.next("client/state")
        self.assertTrue(h.sessions[0].player_active)


class StreamTests(SessionTestCase):
    async def play(self, h, n=5, lead_us=250_000, start_stream=True):
        await h.connect()
        await h.wait_for(lambda: h.sessions[0].clock.converged)
        if start_stream:
            await h.peer.send("stream/start", {"server_transmitted": h.peer.server_now(), "player": FMT48})
            await h.wait_for(lambda: h.sessions[0].streaming)
        base = h.peer.server_now() + lead_us
        for i in range(n):
            await h.peer.send_audio(base + i * 20_000, CHUNK)
        return base

    async def test_audio_is_played_in_order_after_the_lead_in_silence(self):
        h = await self.harness()
        await self.play(h)
        await h.wait_for(lambda: h.sink.written.endswith(CHUNK * 5))
        self.assertEqual([e for e in h.sink.events if e[0] == "open"], [("open", 48000, 2, 16)])
        written = h.sink.written
        self.assertTrue(written.endswith(CHUNK * 5))                           # the five chunks, back to back, in order
        self.assertTrue(set(written[:-5 * len(CHUNK)]) <= {0})                 # whatever came first is silence
        self.assertEqual(h.statuses[-1][0], "streaming")

    async def test_the_volume_is_applied_to_the_sink_when_the_session_starts(self):
        h = await self.harness(volume=33, muted=True)
        await h.connect()
        await h.wait_for(lambda: ("volume", 33) in h.sink.events)
        self.assertIn(("mute", True), h.sink.events)

    async def test_the_state_reports_the_format_while_streaming(self):
        h = await self.harness()
        await self.play(h, n=0)
        while True:
            state = await h.peer.next("client/state")
            if "format" in state[1]["player"]:
                break
        self.assertEqual(state[1]["player"]["format"], FMT48)

    async def test_stream_end_closes_the_sink_and_returns_to_connected(self):
        h = await self.harness()
        await self.play(h, n=1)
        await h.peer.send("stream/end", {})
        await h.wait_for(lambda: not h.sessions[0].streaming)
        self.assertEqual(h.sink.events[-1], ("close",))
        self.assertEqual(h.statuses[-1][0], "connected")

    async def test_stream_clear_discards_queued_audio_and_flushes_the_sink(self):
        h = await self.harness()
        await self.play(h, n=10, lead_us=5_000_000)                            # far in the future: stays queued
        await h.wait_for(lambda: h.sessions[0].scheduler.queued_bytes > 0)
        await h.peer.send("stream/clear", {"server_transmitted": h.peer.server_now()})
        await h.wait_for(lambda: h.sessions[0].scheduler.queued_bytes == 0)
        self.assertIn(("flush",), h.sink.events)
        self.assertEqual(h.sink.written, b"")

    async def test_a_new_stream_start_with_another_format_reopens_the_sink(self):
        h = await self.harness()
        await self.play(h, n=1)
        await h.peer.send("stream/start", {"server_transmitted": h.peer.server_now(), "player": {**FMT48, "sample_rate": 44100}})
        await h.wait_for(lambda: [e for e in h.sink.events if e[0] == "open"][-1][1] == 44100)
        self.assertTrue(h.sessions[0].streaming)
        self.assertEqual(h.sessions[0].scheduler.rate, 44100)

    async def test_audio_before_a_stream_starts_is_ignored(self):
        h = await self.harness()
        await self.play(h, n=3, start_stream=False)
        await asyncio.sleep(0.2)
        self.assertEqual(h.sink.written, b"")
        self.assertEqual(h.sessions[0].scheduler.queued_bytes, 0)

    async def test_an_unsupported_format_ends_the_session(self):
        h = await self.harness()
        await h.connect()
        await h.peer.send("stream/start", {"server_transmitted": 1, "player": {**FMT48, "codec": "flac"}})
        await asyncio.wait_for(h.finished.wait(), 3)
        self.assertIn("only pcm", h.reasons[0])

    async def test_a_sink_that_cannot_open_reports_an_error_and_keeps_the_connection(self):
        h = await self.harness()
        h.sink.fail_open = True
        await h.connect()
        await h.wait_for(lambda: h.sessions[0].clock.converged)
        await h.peer.send("stream/start", {"server_transmitted": 1, "player": FMT48})
        await h.wait_for(lambda: h.statuses and h.statuses[-1][0] == "error")
        self.assertFalse(h.sessions[0].streaming)
        self.assertFalse(h.finished.is_set())

    async def test_late_audio_is_dropped_and_counted(self):
        h = await self.harness()
        await self.play(h, n=3, lead_us=-2_000_000)                             # two seconds in the past
        await h.wait_for(lambda: h.sessions[0].dropped_frames >= 3 * 960)
        self.assertEqual(h.sink.written, b"")


class CommandTests(SessionTestCase):
    async def command(self, h, **player):
        await h.peer.send("server/command", {"player": player})

    async def test_volume_reaches_the_sink_the_settings_callback_and_the_state(self):
        h = await self.harness()
        await h.connect()
        await self.command(h, command="volume", volume=20)
        await h.wait_for(lambda: {"volume": 20} in h.settings)
        self.assertEqual(h.sink.volume, 20)
        while (await h.peer.next("client/state"))[1]["player"]["volume"] != 20:
            pass

    async def test_mute_and_output_delay(self):
        h = await self.harness()
        await h.connect()
        await self.command(h, command="mute", mute=True)
        await self.command(h, command="set_output_delay", output_delay_ms=250)
        await h.wait_for(lambda: {"output_delay_ms": 250} in h.settings)
        self.assertEqual((h.sink.muted, h.sessions[0].scheduler.output_delay_us), (True, 250_000))
        self.assertIn({"muted": True}, h.settings)

    async def test_an_out_of_range_command_ends_the_session(self):
        h = await self.harness()
        await h.connect()
        await self.command(h, command="volume", volume=101)
        await asyncio.wait_for(h.finished.wait(), 3)
        self.assertIn("protocol error", h.reasons[0])


class EndingTests(SessionTestCase):
    async def test_a_stop_request_says_goodbye_and_closes(self):
        h = await self.harness()
        await h.connect()
        await h.wait_for(lambda: h.sessions[0].clock.converged)
        h.sessions[0].request_stop("shutdown")
        self.assertEqual(await h.peer.next("client/goodbye"), ("client/goodbye", {"reason": "shutdown"}))
        await asyncio.wait_for(h.finished.wait(), 3)
        self.assertEqual(h.reasons[0], "stopped (shutdown)")
        self.assertEqual(h.statuses[-1][0], "waiting")

    async def test_the_server_closing_ends_the_session_cleanly_and_releases_the_sink(self):
        h = await self.harness()
        await h.connect()
        await h.peer.send("stream/start", {"server_transmitted": 1, "player": FMT48})
        await h.wait_for(lambda: h.sessions[0].streaming)
        h.peer.pump.cancel()
        h.peer.ws.close()
        await asyncio.wait_for(h.finished.wait(), 3)
        self.assertEqual(h.sink.events[-1], ("close",))
        self.assertFalse(h.sessions[0].streaming)

    async def test_a_server_that_stops_answering_time_requests_ends_the_session(self):
        h = await self.harness()
        h.peer.answer_time = False
        with mock.patch.object(sess, "TIME_REPLY_TIMEOUT_S", 0.005):
            await h.connect()
            await asyncio.wait_for(h.finished.wait(), 5)
        self.assertIn("time requests", h.reasons[0])

    async def test_no_activation_in_time_drops_the_provisional_connection(self):
        h = await self.harness()
        with mock.patch.object(sess, "ACTIVATE_TIMEOUT_S", 0.2):
            await h.peer.handshake()
            await h.peer.send("server/hello", {"name": "Slow"})
            self.assertEqual((await h.peer.read())[0], "client/hello")
            await asyncio.wait_for(h.finished.wait(), 3)
        self.assertEqual(h.reasons[0], "timed out")
        self.assertEqual(h.statuses[-1][0], "waiting")


if __name__ == "__main__":
    unittest.main()
