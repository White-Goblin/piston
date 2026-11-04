import asyncio
import unittest
from unittest import mock

from piston_device.sendspin import codecs, messages as sm, session as sess
from tests import sendspin_flac_fixtures as fx
from tests.sendspin_opus_fixtures import REAL_PACKETS
from tests.test_sendspin_session import SessionTestCase

REAL_FLAC_HEADER_B64 = "ZkxhQ4AAACISABIAAAAAAG5aCsRDcAAAAAAAAAAAAAAAAAAAAAAAAAAA"
FLAC24 = {"codec": "flac", "sample_rate": 44100, "channels": 2, "bit_depth": 24, "codec_header": REAL_FLAC_HEADER_B64}
OPUS = {"codec": "opus", "sample_rate": 48000, "channels": 2, "bit_depth": 16}
PCM = {"codec": "pcm", "sample_rate": 48000, "channels": 2, "bit_depth": 16}
ALL = {"flac", "opus"}
STEP_FLAC_US = 104_490       # 4608 samples at 44.1 kHz
STEP_OPUS_US = 20_000


def flac16_format():
    import base64
    return {"codec": "flac", "sample_rate": 48000, "channels": 2, "bit_depth": 16, "codec_header": base64.b64encode(fx.SINE16_HEADER).decode()}


class CodecSessionTestCase(SessionTestCase):
    async def play(self, h, fmt, chunks, step_us, lead_us=400_000, wait_tail=None):
        await h.connect()
        await h.wait_for(lambda: h.sessions[0].clock.converged)
        await h.peer.send("stream/start", {"server_transmitted": h.peer.server_now(), "player": fmt})
        await h.wait_for(lambda: h.sessions[0].streaming)
        base = h.peer.server_now() + lead_us
        for i, chunk in enumerate(chunks):
            await h.peer.send_audio(base + i * step_us, chunk)
        if wait_tail is not None:
            await h.wait_for(lambda: h.sink.written.endswith(wait_tail), timeout=4)
        return base


@unittest.skipUnless(codecs.available() >= ALL, "libFLAC and libopus are needed on the test host")
class DecodedStreamTests(CodecSessionTestCase):
    async def test_a_real_24_bit_flac_stream_plays_as_32_bit_samples_in_order(self):
        h = await self.harness(available=ALL)
        decoder = codecs.FlacDecoder(fx.REAL_HEADER, 44100, 2, 24)
        expected = decoder.decode(fx.REAL_CHUNKS[0]) + decoder.decode(fx.REAL_CHUNKS[1])
        await self.play(h, FLAC24, fx.REAL_CHUNKS, STEP_FLAC_US, wait_tail=expected)
        self.assertEqual([e for e in h.sink.events if e[0] == "open"], [("open", 44100, 2, 32)])
        self.assertEqual(h.sessions[0].scheduler.frame_bytes, 8)
        self.assertEqual(h.sessions[0].scheduler.decode_errors, 0)

    async def test_a_16_bit_flac_stream_plays_as_16_bit_samples(self):
        h = await self.harness(available=ALL)
        expected = codecs.FlacDecoder(fx.SINE16_HEADER, 48000, 2, 16).decode(fx.SINE16_FRAMES)
        await self.play(h, flac16_format(), [fx.SINE16_FRAMES], STEP_FLAC_US, wait_tail=expected)
        self.assertEqual([e for e in h.sink.events if e[0] == "open"], [("open", 48000, 2, 16)])
        self.assertEqual(h.sessions[0].scheduler.frame_bytes, 4)

    async def test_an_opus_stream_plays_with_the_decoders_state_carried_from_packet_to_packet(self):
        h = await self.harness(available=ALL)
        reference = codecs.OpusDecoder()
        expected = b"".join(reference.decode(p) for p in REAL_PACKETS)
        await self.play(h, OPUS, REAL_PACKETS, STEP_OPUS_US, wait_tail=expected)
        self.assertEqual([e for e in h.sink.events if e[0] == "open"], [("open", 48000, 2, 16)])

    async def test_the_state_reports_the_codec_that_is_playing(self):
        h = await self.harness(available=ALL)
        await self.play(h, OPUS, REAL_PACKETS[:3], STEP_OPUS_US)
        while True:
            state = await h.peer.next("client/state")
            if "format" in state[1]["player"]:
                break
        self.assertEqual(state[1]["player"]["format"]["codec"], "opus")

    async def test_the_hello_offers_exactly_what_this_device_can_decode(self):
        h = await self.harness(available=ALL)
        await h.connect()
        self.assertEqual(h.peer.hello["player@v1_support"]["supported_formats"], sm.offered_formats(ALL))

    async def test_a_corrupt_chunk_is_dropped_and_the_music_goes_on(self):
        h = await self.harness(available=ALL)
        decoder = codecs.FlacDecoder(fx.REAL_HEADER, 44100, 2, 24)
        good = decoder.decode(fx.REAL_CHUNKS[1])
        chunks = [fx.REAL_CHUNKS[0], b"\x00" * 200, fx.REAL_CHUNKS[1]]
        await self.play(h, FLAC24, chunks, STEP_FLAC_US, wait_tail=good)
        self.assertEqual(h.sessions[0].scheduler.decode_errors, 1)
        self.assertTrue(h.sessions[0].streaming)

    async def test_a_burst_of_corrupt_chunks_ends_the_stream_with_an_error(self):
        h = await self.harness(available=ALL)
        await self.play(h, FLAC24, [bytes([i]) * 100 for i in range(sess.DECODE_FAILURE_LIMIT + 5)], 10_000, lead_us=150_000)
        await h.wait_for(lambda: h.statuses and h.statuses[-1][0] == "error", timeout=4)
        self.assertEqual(h.statuses[-1][1]["error"], "decode failed")
        self.assertFalse(h.sessions[0].streaming)
        self.assertEqual(h.sink.events[-1], ("close",))
        self.assertFalse(h.finished.is_set())                         # the connection itself stays up

    async def test_failures_spread_over_time_do_not_end_the_stream(self):
        h = await self.harness(available=ALL)
        now = [100.0]
        sessions_clock = lambda: now[0]
        await h.connect()
        s = h.sessions[0]
        s._monotonic = sessions_clock
        for _ in range(sess.DECODE_FAILURE_LIMIT + 10):                # more failures in total than the limit, but 1 s apart
            s.scheduler.decode_errors += 1
            now[0] += 1.0
            self.assertFalse(await s._decode_failed_too_often())
        s.scheduler.decode_errors += sess.DECODE_FAILURE_LIMIT        # a burst
        self.assertTrue(await s._decode_failed_too_often())

    async def test_a_format_change_from_flac_to_pcm_reopens_the_sink_and_drops_the_decoder(self):
        h = await self.harness(available=ALL)
        await self.play(h, FLAC24, fx.REAL_CHUNKS, STEP_FLAC_US)
        await h.peer.send("stream/start", {"server_transmitted": h.peer.server_now(), "player": PCM})
        await h.wait_for(lambda: [e for e in h.sink.events if e[0] == "open"][-1] == ("open", 48000, 2, 16))
        self.assertIsNone(h.sessions[0]._decoder)
        self.assertIsNone(h.sessions[0].scheduler.decode)
        self.assertEqual(h.sessions[0].scheduler.frame_bytes, 4)

    async def test_a_clear_resets_the_opus_decoder(self):
        h = await self.harness(available=ALL)
        with mock.patch.object(codecs.OpusDecoder, "reset", autospec=True, side_effect=codecs.OpusDecoder.reset) as reset:
            await self.play(h, OPUS, REAL_PACKETS[:3], STEP_OPUS_US, lead_us=5_000_000)
            await h.peer.send("stream/clear", {"server_transmitted": h.peer.server_now()})
            await h.wait_for(lambda: reset.call_count >= 1)

    async def test_the_decoder_is_released_when_the_stream_and_the_session_end(self):
        h = await self.harness(available=ALL)
        await self.play(h, OPUS, REAL_PACKETS[:3], STEP_OPUS_US)
        self.assertIsNotNone(h.sessions[0]._decoder)
        await h.peer.send("stream/end", {})
        await h.wait_for(lambda: not h.sessions[0].streaming)
        self.assertIsNone(h.sessions[0]._decoder)


class OfferingTests(CodecSessionTestCase):
    async def test_a_codec_that_was_not_offered_ends_the_session(self):
        h = await self.harness()                                       # nothing available: PCM only
        await h.connect()
        await h.peer.send("stream/start", {"server_transmitted": 1, "player": OPUS})
        await asyncio.wait_for(h.finished.wait(), 3)
        self.assertIn("not offered", h.reasons[0])

    async def test_the_hello_of_a_pcm_only_device_offers_pcm_only(self):
        h = await self.harness()
        await h.connect()
        self.assertEqual({f["codec"] for f in h.peer.hello["player@v1_support"]["supported_formats"]}, {"pcm"})

    async def test_a_flac_header_that_disagrees_with_the_stream_ends_the_session(self):
        h = await self.harness(available=ALL)
        await h.connect()
        await h.peer.send("stream/start", {"server_transmitted": 1, "player": {**FLAC24, "sample_rate": 48000}})
        await asyncio.wait_for(h.finished.wait(), 3)
        self.assertIn("protocol error", h.reasons[0])


if __name__ == "__main__":
    unittest.main()
