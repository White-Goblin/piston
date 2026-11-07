"""FLAC and Opus decoding through ctypes over the system libFLAC and libopus (no dependencies, like crypto.py).

This module holds the loading, the declared signatures, the low-level decode calls, and the self-tests that decide which
codecs the player may offer. The decoder classes with their limits and sample conversion are `FlacDecoder` and
`OpusDecoder` (added on top). Measured on the device: both libraries load inside the
Decky backend from /usr/lib and decode at about 0.1-0.2 percent of a core."""
import ctypes
import ctypes.util
from array import array
from typing import NamedTuple

FLAC_SONAMES = ("libFLAC.so.14", "libFLAC.so.12", "libFLAC.so.8")
OPUS_SONAMES = ("libopus.so.0",)
MAX_SAMPLES_PER_CHUNK = 48000 * 400 // 1000  # a chunk may decode to at most 400 ms (per channel)
MAX_FLAC_CHUNK = 256 * 1024  # bytes of FLAC frames in one chunk (the spec caps a chunk at 150 ms)
OPUS_MAX_FRAME = 5760  # 120 ms at 48 kHz
OPUS_RESET_STATE = 4028
MAX_OPUS_PACKET = 8192  # one Opus packet is at most about 1.3 KB per frame; anything larger is not a packet

# Known-answer vectors for the self-tests. FLAC: a 42-byte header (fLaC + STREAMINFO, last-block flag set) and one frame of
# 64 samples, 16-bit stereo, 44.1 kHz, L[i] = (i*37 mod 200) - 100, R[i] = (i*91 mod 300) - 150, made with the reference encoder.
# Opus: one 20 ms stereo packet (the 6th) of a 440 Hz tone with amplitude 8000, made with the libopus encoder.
_FLAC_HEADER = bytes.fromhex(
    "664c6143800000220040004000008c00008c0ac442f000000040ae8c980f5aac"
    "b6f44d005ca76f27ecaa")
_FLAC_FRAME = bytes.fromhex(
    "fff869a8003fe81001879798c46cab548b36599149b35cbca308c6ddaa4b39b0"
    "af26f2394641fcdb48a430b0de2f4d55d44182fdb5d6512b0af562ad56a41fd2"
    "5dbaa509469d2acaa2301b943f2de465036a452c6296b612baf689b82aa59a9d"
    "0927fed03a881f5cc849959127675d2c3296d6b251192ca7915c3d087ca1f93e"
    "119dcda914b1cad6c2540bda")
_OPUS_PACKET = bytes.fromhex(
    "fcac9ca5c93917d410c9ec679d38fd910e6d340d46145b9f6cca6dfd88908ab4"
    "0d5621ab5d84f75aedc59f5de3ca90fe7404d68a13e9a67f00112657d3815f16"
    "8c1c475adef57660dbad2a1e03599c476a01071d634899adc77a619fbe186392"
    "d1e0c9e7e463254e493af3ca31b127e0f6025112a6f1e8b315e37eb296813203"
    "ef1ebfadc637cf0e8fd01742fa0cd16365fa9be189892d62585672dfcf60ff3f"
    "481573683e9725fa987e3e381a84b6f72d61a742ceff8c94c7d1f1653aaab232"
    "27695e53270b06733ec9e74dff8bcff3d516cc5bd9e11a52f3549058acea56ce"
    "1e1ee98ffc01c13fed692b5b57afd1200084d6d6bad253ee")


class CodecError(Exception):
    """A chunk could not be decoded, or a decoder could not be built."""


class CodecUnavailable(Exception):
    """The library could not be loaded, or it failed its self-test; the codec must not be offered."""


_log = lambda message: None
_libs = {}
_available = None


def set_logger(log) -> None:
    global _log
    _log = log


def _load(label: str, names: str, sonames: tuple):
    path = ctypes.util.find_library(names)
    last = None
    for candidate in ([path] if path else []) + list(sonames):
        try:
            return ctypes.CDLL(candidate)
        except OSError as e:
            last = e
    raise CodecUnavailable(f"{label} could not be loaded ({last})")


# --- FLAC ------------------------------------------------------------------------------------------------------------------

_vp = ctypes.c_void_p


class _FrameHeader(ctypes.Structure):  # the start of FLAC__Frame (FLAC__FrameHeader): the fields we read
    _fields_ = [("blocksize", ctypes.c_uint), ("sample_rate", ctypes.c_uint), ("channels", ctypes.c_uint),
                ("channel_assignment", ctypes.c_int), ("bits_per_sample", ctypes.c_uint)]


_READ_CB = ctypes.CFUNCTYPE(ctypes.c_int, _vp, ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(ctypes.c_size_t), _vp)
_WRITE_CB = ctypes.CFUNCTYPE(ctypes.c_int, _vp, ctypes.POINTER(_FrameHeader), ctypes.POINTER(ctypes.POINTER(ctypes.c_int32)), _vp)
_ERROR_CB = ctypes.CFUNCTYPE(None, _vp, ctypes.c_int, _vp)


def load_flac():
    """libFLAC with its signatures declared; cached. Raises CodecUnavailable."""
    if "flac" not in _libs:
        lib = _load("libFLAC", "FLAC", FLAC_SONAMES)
        try:
            lib.FLAC__stream_decoder_new.restype = _vp
            lib.FLAC__stream_decoder_init_stream.restype = ctypes.c_int
            lib.FLAC__stream_decoder_init_stream.argtypes = [_vp, _READ_CB, _vp, _vp, _vp, _vp, _WRITE_CB, _vp, _ERROR_CB, _vp]
            lib.FLAC__stream_decoder_process_until_end_of_stream.restype = ctypes.c_int
            lib.FLAC__stream_decoder_process_until_end_of_stream.argtypes = [_vp]
            lib.FLAC__stream_decoder_finish.argtypes = [_vp]
            lib.FLAC__stream_decoder_delete.argtypes = [_vp]
        except AttributeError as e:
            raise CodecUnavailable(f"libFLAC lacks a function we need ({e})") from None
        _libs["flac"] = lib
    return _libs["flac"]


class FlacFrame(NamedTuple):
    channels: int
    bits: int
    samples: bytes  # interleaved little-endian int32, one value per sample (the libFLAC representation)


def flac_decode_raw(header: bytes, chunk: bytes, max_samples: int = MAX_SAMPLES_PER_CHUNK) -> tuple:
    """Decode `chunk` (whole FLAC frames) with a fresh stream decoder initialised from `header + chunk`.

    Returns (frames, errors, aborted): the decoded frames, how many errors libFLAC reported, and whether the decode was
    aborted because the output would exceed `max_samples`. Raises CodecError when the decoder cannot be built."""
    lib = load_flac()
    data = bytes(header) + bytes(chunk)
    state = {"pos": 0, "samples": 0, "errors": 0, "aborted": False}
    frames = []

    def read(_decoder, buffer, nbytes, _client):
        n = min(nbytes[0], len(data) - state["pos"])
        if n <= 0:
            nbytes[0] = 0
            return 1  # END_OF_STREAM
        ctypes.memmove(buffer, data[state["pos"]:state["pos"] + n], n)
        nbytes[0] = n
        state["pos"] += n
        return 0  # CONTINUE

    def write(_decoder, frame, planes, _client):
        h = frame.contents
        blocksize, channels = h.blocksize, h.channels
        state["samples"] += blocksize
        if not 1 <= channels <= 8 or not 1 <= blocksize <= 65535 or state["samples"] > max_samples:
            state["errors"] += 1
            state["aborted"] = True
            return 1  # ABORT
        interleaved = array("i", bytes(4 * blocksize * channels))
        for c in range(channels):
            interleaved[c::channels] = array("i", ctypes.string_at(planes[c], 4 * blocksize))
        frames.append(FlacFrame(channels, h.bits_per_sample, interleaved.tobytes()))
        return 0

    def error(_decoder, _status, _client):
        state["errors"] += 1

    callbacks = (_READ_CB(read), _WRITE_CB(write), _ERROR_CB(error))  # kept alive until the decoder is finished
    decoder = lib.FLAC__stream_decoder_new()
    if not decoder:
        raise CodecError("libFLAC is out of memory")
    try:
        if lib.FLAC__stream_decoder_init_stream(decoder, callbacks[0], None, None, None, None, callbacks[1], None, callbacks[2], None) != 0:
            raise CodecError("the FLAC decoder could not be initialised")
        lib.FLAC__stream_decoder_process_until_end_of_stream(decoder)
        lib.FLAC__stream_decoder_finish(decoder)
    finally:
        lib.FLAC__stream_decoder_delete(decoder)
    return frames, state["errors"], state["aborted"]


def self_test_flac() -> None:
    """Decode the embedded frame and compare every sample; raises CodecUnavailable on any mismatch."""
    load_flac()
    try:
        frames, errors, aborted = flac_decode_raw(_FLAC_HEADER, _FLAC_FRAME)
    except CodecError as e:
        raise CodecUnavailable(f"FLAC self-test failed: {e}") from None
    expected = array("i")
    for i in range(64):
        expected.extend(((i * 37 % 200) - 100, (i * 91 % 300) - 150))
    if errors or aborted or len(frames) != 1 or frames[0].channels != 2 or frames[0].bits != 16 or frames[0].samples != expected.tobytes():
        raise CodecUnavailable("FLAC self-test failed: the decoded samples differ from the known answer")


def to_container(samples: bytes, bits: int) -> bytes:
    """libFLAC's interleaved int32 samples to the sink's sample format: 16-bit audio as s16, 24-bit audio as s32 with the
    value left-justified (shifted left by 8), which `pw-cat --format s32` plays at the right level."""
    if bits == 16:
        out = bytearray(len(samples) // 2)
        out[0::2], out[1::2] = samples[0::4], samples[1::4]
    elif bits == 24:
        out = bytearray(len(samples))
        out[1::4], out[2::4], out[3::4] = samples[0::4], samples[1::4], samples[2::4]
    else:
        raise CodecError(f"unsupported bit depth {bits}")
    return bytes(out)


class FlacDecoder:
    """Decodes the chunks of one FLAC stream (each chunk is one or more whole frames). Stateless between chunks: FLAC
    frames are independent, and a fresh libFLAC decoder per chunk costs the same as a persistent one (measured)."""

    def __init__(self, header: bytes, rate: int, channels: int, bits: int):
        if channels != 2 or bits not in (16, 24) or rate not in (44100, 48000):
            raise CodecError("unsupported FLAC stream format")
        if len(header) != 42 or bytes(header[:4]) != b"fLaC" or not header[4] & 0x80:
            # libFLAC would happily decode bare frames without a header: the header is checked here instead
            raise CodecError("the FLAC header must be 'fLaC' + a STREAMINFO block with the last-block flag set (42 bytes)")
        self.header, self.rate, self.channels, self.bits = bytes(header), rate, channels, bits
        self.sample_format = "s16" if bits == 16 else "s32"
        self.frame_bytes = channels * (2 if bits == 16 else 4)
        self.max_samples = rate * 400 // 1000
        self.errors = 0  # frames libFLAC reported as damaged, over the life of the decoder

    def decode(self, chunk: bytes) -> bytes:
        """The chunk as interleaved PCM in `sample_format`. A damaged frame inside a multi-frame chunk is dropped; a chunk
        that yields no audio, is too large, or decodes to too much, raises CodecError."""
        if not 1 <= len(chunk) <= MAX_FLAC_CHUNK:
            raise CodecError("bad FLAC chunk size")
        frames, errors, aborted = flac_decode_raw(self.header, chunk, self.max_samples)
        self.errors += errors
        if aborted:
            raise CodecError("the FLAC chunk decodes to more audio than a chunk may hold")
        if not frames:
            raise CodecError("no audio decoded from the FLAC chunk")
        if any(f.channels != self.channels or f.bits != self.bits for f in frames):
            raise CodecError("a FLAC frame does not match the stream format")
        return b"".join(to_container(f.samples, self.bits) for f in frames)


# --- Opus ------------------------------------------------------------------------------------------------------------------

def load_opus():
    """libopus with its signatures declared; cached. Raises CodecUnavailable."""
    if "opus" not in _libs:
        lib = _load("libopus", "opus", OPUS_SONAMES)
        try:
            lib.opus_decoder_create.restype = _vp
            lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
            lib.opus_decode.restype = ctypes.c_int
            lib.opus_decode.argtypes = [_vp, ctypes.c_char_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
            lib.opus_decoder_destroy.argtypes = [_vp]
            lib.opus_decoder_ctl.restype = ctypes.c_int
            lib.opus_decoder_ctl.argtypes = [_vp, ctypes.c_int]
        except AttributeError as e:
            raise CodecUnavailable(f"libopus lacks a function we need ({e})") from None
        _libs["opus"] = lib
    return _libs["opus"]


def opus_open(rate: int = 48000, channels: int = 2):
    lib = load_opus()
    error = ctypes.c_int(0)
    handle = lib.opus_decoder_create(rate, channels, ctypes.byref(error))
    if not handle or error.value != 0:
        raise CodecError(f"opus_decoder_create failed ({error.value})")
    return handle


def opus_decode_raw(handle, packet: bytes, channels: int = 2) -> bytes:
    """One packet to interleaved little-endian s16. Negative results from libopus raise CodecError. An empty packet is
    refused here: libopus would treat it as a lost packet and return concealment audio instead of an error."""
    if not 1 <= len(packet) <= MAX_OPUS_PACKET:
        raise CodecError("bad Opus packet size")
    lib = load_opus()
    out = (ctypes.c_int16 * (OPUS_MAX_FRAME * channels))()
    n = lib.opus_decode(handle, bytes(packet), len(packet), out, OPUS_MAX_FRAME, 0)
    if n < 0:
        raise CodecError(f"opus_decode failed ({n})")
    return ctypes.string_at(out, n * channels * 2)


def opus_reset(handle) -> None:
    load_opus().opus_decoder_ctl(handle, OPUS_RESET_STATE)


def opus_close(handle) -> None:
    if handle:
        load_opus().opus_decoder_destroy(handle)


def self_test_opus() -> None:
    """Decode the embedded packet: 960 frames of audible, bounded sound (a cold decoder decodes a mid-stream packet at a lower
    level than the tone it came from, so the check is a range, not a number); raises CodecUnavailable otherwise."""
    load_opus()
    handle = None
    try:
        handle = opus_open()
        pcm = opus_decode_raw(handle, _OPUS_PACKET)
    except CodecError as e:
        raise CodecUnavailable(f"Opus self-test failed: {e}") from None
    finally:
        opus_close(handle)
    samples = array("h")
    samples.frombytes(pcm)
    left = samples[0::2]
    rms = (sum(float(v) * v for v in left) / max(1, len(left))) ** 0.5
    if len(left) != 960 or not 1000 <= rms <= 8000:
        raise CodecUnavailable(f"Opus self-test failed: {len(left)} frames, level {rms:.0f}")


# --- availability ----------------------------------------------------------------------------------------------------------

def available() -> set:
    """The codecs whose library loaded and passed its self-test ("flac", "opus"); computed once, a failure is logged once."""
    global _available
    if _available is None:
        found = set()
        for name, test in (("flac", self_test_flac), ("opus", self_test_opus)):
            try:
                test()
                found.add(name)
            except Exception as e:  # noqa: BLE001 - whatever goes wrong, that codec is simply not offered
                _log(f"Sendspin: {name} decoding is not available: {e}")
        _available = frozenset(found)
    return set(_available)


def reset_for_tests() -> None:
    global _available
    _available = None
    _libs.clear()


class OpusDecoder:
    """Decodes the packets of one Opus stream (exactly one packet per chunk, 48 kHz stereo, output s16). Unlike FLAC, Opus
    keeps state from packet to packet, so one decoder lives as long as the stream and `reset()` is called after a clear."""

    sample_format = "s16"
    frame_bytes = 4

    def __init__(self, rate: int = 48000, channels: int = 2):
        if rate != 48000 or channels != 2:
            raise CodecError("unsupported Opus stream format")
        self.rate, self.channels = rate, channels
        self._handle = opus_open(rate, channels)

    def decode(self, packet: bytes) -> bytes:
        """The packet as interleaved s16 PCM (a whole number of frames; at most 120 ms)."""
        if self._handle is None:
            raise CodecError("the Opus decoder is closed")
        return opus_decode_raw(self._handle, packet, self.channels)

    def reset(self) -> None:
        if self._handle is not None:
            opus_reset(self._handle)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            opus_close(handle)

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 - at interpreter shutdown the library may already be gone
            pass
