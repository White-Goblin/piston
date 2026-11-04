import ctypes as C
import math
import random
import time
import unittest
from array import array

from piston_device.sendspin import codecs
from tests.sendspin_opus_fixtures import REAL_PACKETS

try:
    codecs.load_opus()
    HAVE = True
except codecs.CodecUnavailable:
    HAVE = False


def encode_tone(seconds=1.0, freq=440, amplitude=12000, packets=None):
    """Packets of a stereo sine made with the libopus encoder (20 ms each)."""
    lib = codecs.load_opus()
    lib.opus_encoder_create.restype = C.c_void_p
    lib.opus_encoder_create.argtypes = [C.c_int32, C.c_int, C.c_int, C.POINTER(C.c_int)]
    lib.opus_encode.restype = C.c_int32
    lib.opus_encode.argtypes = [C.c_void_p, C.POINTER(C.c_int16), C.c_int, C.c_char_p, C.c_int32]
    lib.opus_encoder_destroy.argtypes = [C.c_void_p]
    err = C.c_int(0)
    enc = lib.opus_encoder_create(48000, 2, 2049, C.byref(err))
    count = packets or int(seconds * 50)
    samples = array("h")
    for i in range(count * 960):
        v = int(amplitude * math.sin(2 * math.pi * freq * i / 48000))
        samples.extend((v, v))
    out, buf = [], C.create_string_buffer(4000)
    try:
        for i in range(count):
            pcm = (C.c_int16 * 1920).from_buffer_copy(samples[i * 1920:(i + 1) * 1920].tobytes())
            n = lib.opus_encode(enc, pcm, 960, buf, 4000)       # encode first: `buf.raw` must be read after the call
            out.append(buf.raw[:n])
    finally:
        lib.opus_encoder_destroy(enc)
    return out


def left_channel(pcm):
    a = array("h")
    a.frombytes(pcm)
    return a[0::2]


def rms(values):
    return math.sqrt(sum(float(v) * v for v in values) / max(1, len(values)))


@unittest.skipUnless(HAVE, "libopus is needed on the test host")
class OpusDecoderTests(unittest.TestCase):
    def test_a_tone_survives_the_round_trip(self):
        decoder = codecs.OpusDecoder()
        decoded = b"".join(decoder.decode(p) for p in encode_tone(1.0))
        left = left_channel(decoded)
        self.assertEqual(len(left), 48000)                                            # exactly 960 frames per packet
        tail = left[4800:]                                                            # skip the encoder's start-up
        crossings = sum(1 for i in range(1, len(tail)) if tail[i - 1] < 0 <= tail[i])
        self.assertAlmostEqual(crossings / (len(tail) / 48000), 440, delta=3)
        self.assertAlmostEqual(rms(tail) / (12000 / math.sqrt(2)), 1.0, delta=0.1)     # level preserved within 10%

    def test_real_music_assistant_packets_decode_to_960_frames_of_plausible_audio(self):
        decoder = codecs.OpusDecoder()
        levels = []
        for packet in REAL_PACKETS:
            pcm = decoder.decode(packet)
            self.assertEqual(len(pcm), 960 * 4)
            levels.append(rms(left_channel(pcm)))
        self.assertTrue(all(l < 32768 for l in levels))
        self.assertGreater(sum(levels) / len(levels), 100)                            # music, not silence

    def test_the_decoder_describes_its_output(self):
        decoder = codecs.OpusDecoder()
        self.assertEqual((decoder.sample_format, decoder.frame_bytes), ("s16", 4))

    def test_reset_makes_the_next_packet_independent_of_earlier_ones(self):
        used, fresh = codecs.OpusDecoder(), codecs.OpusDecoder()
        for packet in REAL_PACKETS[:10]:
            used.decode(packet)
        used.reset()
        self.assertEqual(used.decode(REAL_PACKETS[20]), fresh.decode(REAL_PACKETS[20]))

    def test_state_does_carry_over_between_packets_without_a_reset(self):
        a, b = codecs.OpusDecoder(), codecs.OpusDecoder()
        for packet in REAL_PACKETS[:10]:
            a.decode(packet)
        self.assertNotEqual(a.decode(REAL_PACKETS[20]), b.decode(REAL_PACKETS[20]))   # which is why clear() must reset it

    def test_bad_packet_sizes_are_refused(self):
        decoder = codecs.OpusDecoder()
        for packet in (b"", bytes(codecs.MAX_OPUS_PACKET + 1)):
            with self.assertRaises(codecs.CodecError):
                decoder.decode(packet)

    def test_unsupported_formats_are_refused_at_construction(self):
        for args in ((44100, 2), (48000, 1), (24000, 2), (48000, 6)):
            with self.assertRaises(codecs.CodecError, msg=args):
                codecs.OpusDecoder(*args)

    def test_closing_is_idempotent_and_a_closed_decoder_refuses_to_decode(self):
        decoder = codecs.OpusDecoder()
        decoder.close()
        decoder.close()
        decoder.reset()
        with self.assertRaises(codecs.CodecError):
            decoder.decode(REAL_PACKETS[0])

    def test_random_truncated_and_mutated_packets_never_crash_or_hang_and_stay_bounded(self):
        rng = random.Random(99)
        decoder = codecs.OpusDecoder()
        started = time.perf_counter()
        decoded = rejected = 0
        for _ in range(5000):
            base = bytearray(rng.choice(REAL_PACKETS))
            kind = rng.randrange(5)
            if kind == 0:
                data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 600)))
            elif kind == 1:
                data = bytes(base[:rng.randrange(len(base) + 1)])
            elif kind == 2:
                for _ in range(rng.randrange(1, 10)):
                    base[rng.randrange(len(base))] ^= 1 << rng.randrange(8)
                data = bytes(base)
            elif kind == 3:
                data = bytes(base) + bytes(rng.randrange(256) for _ in range(rng.randrange(1, 100)))
            else:
                data = bytes([rng.randrange(256)]) + bytes(base[1:])
            try:
                pcm = decoder.decode(data)
                decoded += 1
                self.assertEqual(len(pcm) % 4, 0)
                self.assertLessEqual(len(pcm), codecs.OPUS_MAX_FRAME * 4)             # never more than 120 ms
            except codecs.CodecError:
                rejected += 1
        self.assertGreater(decoded, 500)
        self.assertGreater(rejected, 50)
        self.assertLess(time.perf_counter() - started, 20)

    def test_many_decoders_can_be_created_and_dropped_without_leaking_handles(self):
        for _ in range(500):
            d = codecs.OpusDecoder()
            d.decode(REAL_PACKETS[0])
            del d


if __name__ == "__main__":
    unittest.main()
