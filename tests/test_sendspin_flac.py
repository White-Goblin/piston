import hashlib
import random
import time
import unittest
from array import array

from piston_device.sendspin import codecs
from tests import sendspin_flac_fixtures as fx

try:
    codecs.load_flac()
    HAVE = True
except codecs.CodecUnavailable:
    HAVE = False

REAL_FRAMES = 4608
sha = lambda b: hashlib.sha256(b).hexdigest()


def real_decoder():
    return codecs.FlacDecoder(fx.REAL_HEADER, 44100, 2, 24)


@unittest.skipUnless(HAVE, "libFLAC is needed on the test host")
class RealMusicAssistantChunkTests(unittest.TestCase):
    def test_each_real_24_bit_chunk_decodes_bit_exactly_against_the_reference_decoder(self):
        decoder = real_decoder()
        for chunk, expected in zip(fx.REAL_CHUNKS, fx.REAL_SHA256):
            pcm = decoder.decode(chunk)
            self.assertEqual(sha(pcm), expected)
            self.assertEqual(len(pcm), REAL_FRAMES * 8)                           # one frame of 4608 samples, 8 bytes per stereo frame

    def test_the_decoder_describes_its_output(self):
        decoder = real_decoder()
        self.assertEqual((decoder.sample_format, decoder.frame_bytes), ("s32", 8))

    def test_two_real_chunks_in_one_chunk_decode_as_the_two_frames_in_order(self):
        pcm = real_decoder().decode(fx.REAL_CHUNKS[0] + fx.REAL_CHUNKS[1])
        self.assertEqual(sha(pcm), fx.REAL_BOTH_SHA256)

    def test_the_24_bit_value_is_left_justified_in_the_low_byte_zero_container(self):
        pcm = real_decoder().decode(fx.REAL_CHUNKS[1])
        self.assertEqual(set(pcm[0::4]), {0})                                   # the low byte of every s32 sample is zero (value << 8)
        self.assertGreater(len(set(pcm[3::4])), 1)                              # and real music uses the high byte

    def test_the_same_decoder_can_be_used_for_many_chunks_in_any_order(self):
        decoder = real_decoder()
        for chunk in (fx.REAL_CHUNKS * 5)[::-1] + fx.REAL_CHUNKS:
            self.assertEqual(len(decoder.decode(chunk)), REAL_FRAMES * 8)
        self.assertEqual(decoder.errors, 0)


@unittest.skipUnless(HAVE, "libFLAC is needed on the test host")
class SixteenBitTests(unittest.TestCase):
    def setUp(self):
        self.decoder = codecs.FlacDecoder(fx.SINE16_HEADER, 48000, 2, 16)

    def test_a_three_frame_chunk_round_trips_bit_exactly_as_s16(self):
        pcm = self.decoder.decode(fx.SINE16_FRAMES)
        left, right = fx.sine16_samples()
        expected = array("h")
        for l, r in zip(left, right):
            expected.extend((l, r))
        self.assertEqual(pcm, expected.tobytes())
        self.assertEqual((self.decoder.sample_format, self.decoder.frame_bytes), ("s16", 4))
        self.assertEqual(len(pcm), 3000 * 4)

    def test_a_decoder_for_the_other_depth_refuses_the_stream(self):
        with self.assertRaises(codecs.CodecError):
            codecs.FlacDecoder(fx.SINE16_HEADER, 48000, 2, 24).decode(fx.SINE16_FRAMES)
        with self.assertRaises(codecs.CodecError):
            real_decoder().__class__(fx.REAL_HEADER, 44100, 2, 16).decode(fx.REAL_CHUNKS[0])


@unittest.skipUnless(HAVE, "libFLAC is needed on the test host")
class LimitAndErrorTests(unittest.TestCase):
    def test_unsupported_stream_formats_are_refused_at_construction(self):
        for args in ((44100, 1, 24), (96000, 2, 24), (44100, 2, 32), (44100, 2, 8), (22050, 2, 16)):
            with self.assertRaises(codecs.CodecError, msg=args):
                codecs.FlacDecoder(fx.REAL_HEADER, *args)

    def test_empty_and_oversized_chunks_are_refused(self):
        decoder = real_decoder()
        for chunk in (b"", bytes(codecs.MAX_FLAC_CHUNK + 1)):
            with self.assertRaises(codecs.CodecError):
                decoder.decode(chunk)

    def test_a_chunk_with_no_audio_raises(self):
        decoder = real_decoder()
        for chunk in (b"\x00" * 64, b"\xff\xf8" + b"\x12" * 300, fx.REAL_CHUNKS[0][:100], fx.REAL_CHUNKS[1][5:], b"not flac at all"):
            with self.assertRaises(codecs.CodecError, msg=len(chunk)):
                decoder.decode(chunk)

    def test_a_chunk_that_decodes_to_more_than_the_budget_is_an_error_not_a_truncation(self):
        decoder = real_decoder()                                                # budget: 400 ms = 17640 samples; five frames are 23040
        with self.assertRaises(codecs.CodecError):
            decoder.decode(fx.REAL_CHUNKS[0] * 5)
        self.assertEqual(len(decoder.decode(fx.REAL_CHUNKS[0] * 3)), 3 * REAL_FRAMES * 8)         # 13824 samples fit

    def test_a_damaged_frame_among_good_ones_is_dropped_and_counted(self):
        decoder = real_decoder()
        bad = bytearray(fx.REAL_CHUNKS[0])
        bad[len(bad) // 2] ^= 0xFF                                              # damage the first frame (the frame CRC catches it)
        pcm = decoder.decode(bytes(bad) + fx.REAL_CHUNKS[1])
        self.assertEqual(sha(pcm), fx.REAL_SHA256[1])                           # only the intact frame came out
        self.assertGreaterEqual(decoder.errors, 1)

    def test_the_output_is_always_whole_frames(self):
        decoder = real_decoder()
        rng = random.Random(7)
        for _ in range(300):
            data = bytearray(rng.choice(fx.REAL_CHUNKS))
            for _ in range(rng.randrange(1, 6)):
                data[rng.randrange(len(data))] ^= 1 << rng.randrange(8)
            try:
                pcm = decoder.decode(bytes(data))
            except codecs.CodecError:
                continue
            self.assertEqual(len(pcm) % decoder.frame_bytes, 0)

    def test_random_truncated_and_mutated_input_never_crashes_or_hangs(self):
        rng = random.Random(1234)
        started = time.perf_counter()
        decoders = [real_decoder(), codecs.FlacDecoder(fx.SINE16_HEADER, 48000, 2, 16)]
        sources = fx.REAL_CHUNKS + [fx.SINE16_FRAMES]
        accepted = rejected = 0
        for i in range(2500):
            dec = rng.choice(decoders)
            base = bytearray(rng.choice(sources))
            kind = rng.randrange(6)
            if kind == 0:
                data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 300)))
            elif kind == 1:
                data = bytes(base[:rng.randrange(len(base) + 1)])
            elif kind == 2:
                for _ in range(rng.randrange(1, 12)):
                    base[rng.randrange(len(base))] ^= 1 << rng.randrange(8)
                data = bytes(base)
            elif kind == 3:
                data = bytes(base) + bytes(rng.randrange(256) for _ in range(rng.randrange(1, 200)))
            elif kind == 4:
                data = bytes(len(base))
            else:
                data = bytes(base[rng.randrange(len(base)):])
            try:
                pcm = dec.decode(data)
                accepted += 1
                self.assertEqual(len(pcm) % dec.frame_bytes, 0)
            except codecs.CodecError:
                rejected += 1
        self.assertGreater(rejected, 100)
        self.assertLess(time.perf_counter() - started, 20)                      # no hang (typically well under a second)

    def test_a_header_of_the_wrong_shape_cannot_start_a_decoder(self):
        no_flag = fx.REAL_HEADER[:4] + bytes([fx.REAL_HEADER[4] & 0x7F]) + fx.REAL_HEADER[5:]
        for header in (b"", b"fLaC", b"x" * 42, fx.REAL_HEADER[:20], fx.REAL_HEADER[:-1], fx.REAL_HEADER + b"\x00", no_flag):
            with self.assertRaises(codecs.CodecError, msg=len(header)):
                codecs.FlacDecoder(header, 44100, 2, 24)

    def test_a_streaminfo_block_full_of_garbage_does_not_crash_the_decoder(self):
        garbage = fx.REAL_HEADER[:8] + bytes(range(34))
        decoder = codecs.FlacDecoder(garbage, 44100, 2, 24)
        try:
            decoder.decode(fx.REAL_CHUNKS[0])
        except codecs.CodecError:
            pass


class ConversionTests(unittest.TestCase):
    def test_16_bit_values_become_s16(self):
        raw = array("i", [1, -1, 32767, -32768]).tobytes()
        self.assertEqual(codecs.to_container(raw, 16), array("h", [1, -1, 32767, -32768]).tobytes())

    def test_24_bit_values_become_left_justified_s32(self):
        values = [0, 1, -1, 8388607, -8388608, 123456, -654321]
        out = array("i")
        out.frombytes(codecs.to_container(array("i", values).tobytes(), 24))
        self.assertEqual(list(out), [v << 8 for v in values])

    def test_other_depths_are_refused(self):
        with self.assertRaises(codecs.CodecError):
            codecs.to_container(bytes(8), 20)


if __name__ == "__main__":
    unittest.main()
