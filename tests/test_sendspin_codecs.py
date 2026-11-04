import unittest
from array import array
from unittest import mock

from piston_device.sendspin import codecs

HAVE_FLAC = HAVE_OPUS = False
try:
    codecs.load_flac()
    HAVE_FLAC = True
except codecs.CodecUnavailable:
    pass
try:
    codecs.load_opus()
    HAVE_OPUS = True
except codecs.CodecUnavailable:
    pass


class Isolated(unittest.TestCase):
    def setUp(self):
        codecs.reset_for_tests()
        self.addCleanup(codecs.reset_for_tests)
        self.addCleanup(codecs.set_logger, lambda message: None)


@unittest.skipUnless(HAVE_FLAC and HAVE_OPUS, "libFLAC and libopus are needed on the test host")
class RealLibraryTests(Isolated):
    def test_both_codecs_are_available_with_the_real_libraries(self):
        self.assertEqual(codecs.available(), {"flac", "opus"})

    def test_the_self_tests_pass(self):
        codecs.self_test_flac()
        codecs.self_test_opus()

    def test_the_embedded_flac_frame_decodes_to_the_known_signal(self):
        frames, errors, aborted = codecs.flac_decode_raw(codecs._FLAC_HEADER, codecs._FLAC_FRAME)
        self.assertEqual((len(frames), errors, aborted), (1, 0, False))
        samples = array("i")
        samples.frombytes(frames[0].samples)
        self.assertEqual((frames[0].channels, frames[0].bits, len(samples)), (2, 16, 128))
        self.assertEqual(list(samples[0:6]), [-100, -150, -63, -59, -26, 32])         # L[i] = i*37 mod 200 - 100, R[i] = i*91 mod 300 - 150
        self.assertEqual(samples[2 * 63], (63 * 37 % 200) - 100)

    def test_the_declared_signatures_are_in_place(self):
        flac, opus = codecs.load_flac(), codecs.load_opus()
        self.assertEqual(flac.FLAC__stream_decoder_new.restype, codecs._vp)
        self.assertEqual(flac.FLAC__stream_decoder_init_stream.argtypes[1], codecs._READ_CB)
        self.assertEqual(opus.opus_decode.restype, codecs.ctypes.c_int)

    def test_a_chunk_over_the_sample_budget_is_aborted(self):
        frames, errors, aborted = codecs.flac_decode_raw(codecs._FLAC_HEADER, codecs._FLAC_FRAME, max_samples=10)
        self.assertEqual((len(frames), errors, aborted), (0, 1, True))

    def test_garbage_after_a_valid_header_yields_no_frames_and_no_crash(self):
        for chunk in (b"", b"\x00" * 100, b"\xff\xf8" + b"\x12" * 200, codecs._FLAC_FRAME[:50]):
            frames, errors, aborted = codecs.flac_decode_raw(codecs._FLAC_HEADER, chunk)
            self.assertEqual(frames, [])

    def test_opus_decodes_a_packet_to_960_frames_and_rejects_garbage_cleanly(self):
        handle = codecs.opus_open()
        try:
            self.assertEqual(len(codecs.opus_decode_raw(handle, codecs._OPUS_PACKET)), 960 * 4)
            with self.assertRaises(codecs.CodecError):
                codecs.opus_decode_raw(handle, b"")
            codecs.opus_reset(handle)
            self.assertEqual(len(codecs.opus_decode_raw(handle, codecs._OPUS_PACKET)), 960 * 4)
        finally:
            codecs.opus_close(handle)
        codecs.opus_close(None)                                                         # closing nothing is harmless

    def test_opus_open_rejects_an_invalid_configuration(self):
        with self.assertRaises(codecs.CodecError):
            codecs.opus_open(rate=44100, channels=2)                                    # Opus has no 44.1 kHz mode

    def test_available_is_computed_once(self):
        first = codecs.available()
        with mock.patch.object(codecs, "self_test_flac", side_effect=AssertionError("must not run again")):
            self.assertEqual(codecs.available(), first)

    def test_the_result_cannot_be_changed_from_outside(self):
        found = codecs.available()
        found.clear()
        self.assertEqual(codecs.available(), {"flac", "opus"})


class FailureTests(Isolated):
    def test_a_library_that_cannot_be_loaded_is_unavailable(self):
        with mock.patch("ctypes.CDLL", side_effect=OSError("no such file")):
            with self.assertRaises(codecs.CodecUnavailable) as cm:
                codecs.load_flac()
            self.assertIn("no such file", str(cm.exception))
            with self.assertRaises(codecs.CodecUnavailable):
                codecs.load_opus()

    def test_the_known_sonames_are_tried_when_find_library_finds_nothing(self):
        tried = []

        def fake(name):
            tried.append(name)
            raise OSError("missing")
        with mock.patch("ctypes.util.find_library", return_value=None), mock.patch("ctypes.CDLL", side_effect=fake):
            with self.assertRaises(codecs.CodecUnavailable):
                codecs.load_flac()
        self.assertEqual(tried, list(codecs.FLAC_SONAMES))

    def test_codecs_that_fail_are_left_out_and_logged_once(self):
        messages = []
        codecs.set_logger(messages.append)
        with mock.patch("ctypes.CDLL", side_effect=OSError("gone")):
            self.assertEqual(codecs.available(), set())
            self.assertEqual(codecs.available(), set())                                  # cached: nothing logged again
        self.assertEqual(len(messages), 2)
        self.assertTrue(any("flac" in m for m in messages) and any("opus" in m for m in messages))

    def test_a_self_test_with_a_wrong_answer_marks_only_that_codec_unavailable(self):
        messages = []
        codecs.set_logger(messages.append)
        with mock.patch.object(codecs, "self_test_flac", side_effect=codecs.CodecUnavailable("wrong samples")):
            found = codecs.available()
        self.assertNotIn("flac", found)
        self.assertEqual(found, {"opus"} if HAVE_OPUS else set())
        self.assertTrue(any("wrong samples" in m for m in messages))

    @unittest.skipUnless(HAVE_FLAC, "libFLAC is needed on the test host")
    def test_a_corrupted_vector_fails_the_flac_self_test(self):
        bad = bytearray(codecs._FLAC_FRAME)
        bad[20] ^= 0x55
        with mock.patch.object(codecs, "_FLAC_FRAME", bytes(bad)):
            with self.assertRaises(codecs.CodecUnavailable):
                codecs.self_test_flac()

    @unittest.skipUnless(HAVE_OPUS, "libopus is needed on the test host")
    def test_a_corrupted_vector_fails_the_opus_self_test(self):
        with mock.patch.object(codecs, "_OPUS_PACKET", b"\x00"):
            with self.assertRaises(codecs.CodecUnavailable):
                codecs.self_test_opus()

    def test_an_unexpected_error_in_a_self_test_does_not_escape_available(self):
        with mock.patch.object(codecs, "self_test_flac", side_effect=RuntimeError("boom")), \
                mock.patch.object(codecs, "self_test_opus", side_effect=ValueError("boom")):
            self.assertEqual(codecs.available(), set())


if __name__ == "__main__":
    unittest.main()
