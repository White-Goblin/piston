"""Sendspin message shapes: the cleartext init frames, the encrypted JSON and binary frames, and the audio chunk.

Exact field names come from swe/notes/sendspin/spec-shapes.md. Every JSON message is `{"type": ..., "payload": {...}}`.
Everything received is validated and bounded; anything malformed raises SendspinProtocolError (the caller closes)."""
import base64
import json
from typing import NamedTuple

PROTOCOL_VERSION = 1
SUITE = "25519_ChaChaPoly_SHA256"
MSG_JSON, MSG_FRAGMENT, MSG_AUDIO = 0, 1, 4
MAX_JSON_BYTES = 16384
MAX_JSON_DEPTH = 10
MAX_NAME_BYTES = 64
MAX_REASSEMBLED = 1_000_000
ID_LEN = 43  # base64url (no padding) of a 32-byte Curve25519 key
AUDIO_HEADER = 13
SEND_AHEAD_UNKNOWN = (0, 4294967295)
PLAYER_COMMANDS = ("volume", "mute", "set_output_delay")
ACTIVITIES = ("playback", "pairing")
GOODBYE_REASONS = ("another_server", "shutdown", "restart", "user_request", "unauthorized", "pairing_required",
                   "concurrent_attempt", "unpaired")
# What this player accepts: raw 16-bit stereo (D6)
PCM_RATES = (48000, 44100)
FLAC_DEPTHS = (24, 16)  # preferred first
OPUS_RATE = 48000       # Opus has no 44.1 kHz mode
FLAC_HEADER_BYTES = 42  # 'fLaC' + the 4-byte block header + the 34-byte STREAMINFO
CODECS = ("flac", "pcm", "opus")


class SendspinProtocolError(Exception):
    """A message violated the Sendspin protocol or our limits."""


class AudioChunk(NamedTuple):
    timestamp_us: int  # server clock, first sample
    send_ahead_us: int  # 0 or 4294967295 = not measured
    pcm: bytes


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise SendspinProtocolError(message)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text) -> bytes:
    _require(isinstance(text, str) and text.isascii(), "expected a base64url string")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        raise SendspinProtocolError("invalid base64url") from None


def client_id_for(public_key: bytes) -> str:
    _require(len(public_key) == 32, "public key must be 32 bytes")
    return _b64url_encode(public_key)


def public_key_from_id(identifier) -> bytes:
    """The 32-byte key behind a client_id/server_id (43 characters, no padding)."""
    _require(isinstance(identifier, str) and len(identifier) == ID_LEN, "id must be 43 characters")
    key = _b64url_decode(identifier)
    _require(len(key) == 32 and _b64url_encode(key) == identifier, "id is not a canonical Curve25519 key")
    return key


def _depth(value, level: int = 1) -> int:
    if isinstance(value, dict):
        return max([level] + [_depth(v, level + 1) for v in value.values()])
    if isinstance(value, list):
        return max([level] + [_depth(v, level + 1) for v in value])
    return level


def _loads(raw: bytes) -> dict:
    _require(len(raw) <= MAX_JSON_BYTES, "JSON message too large")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SendspinProtocolError("invalid JSON") from None
    _require(isinstance(obj, dict), "message must be an object")
    _require(_depth(obj) <= MAX_JSON_DEPTH, "JSON nests too deeply")
    _require(isinstance(obj.get("type"), str) and obj["type"], "message needs a type")
    payload = obj.get("payload", {})
    _require(isinstance(payload, dict), "payload must be an object")
    return obj


def _dumps(message_type: str, payload: dict) -> str:
    return json.dumps({"type": message_type, "payload": payload}, separators=(",", ":"), ensure_ascii=False)


# --- cleartext phase ----------------------------------------------------------------------------------------------

def build_client_init(client_id: str) -> str:
    return _dumps("client/init", {"client_id": client_id, "version": PROTOCOL_VERSION, "suite": SUITE})


def parse_server_init(text: str) -> str:
    """The server_id of a `server/init` text frame; a `server/error` frame raises with its reason."""
    _require(isinstance(text, str), "init frames are text")
    obj = _loads(text.encode("utf-8"))
    payload = obj.get("payload", {})
    if obj["type"] == "server/error":
        raise SendspinProtocolError(f"server refused the connection: {payload.get('reason', 'unknown')}")
    _require(obj["type"] == "server/init", "expected server/init")
    _require(payload.get("version") == PROTOCOL_VERSION and _is_int(payload.get("version")), "unsupported Sendspin version")
    public_key_from_id(payload.get("server_id"))
    return payload["server_id"]


def prologue(client_init_text: str, server_init_text: str) -> bytes:
    """The Noise prologue: the exact bytes of the two init frames as sent and received."""
    return client_init_text.encode("utf-8") + server_init_text.encode("utf-8")


def build_noise_handshake(data: bytes) -> str:
    return _dumps("noise/handshake", {"data": _b64url_encode(data)})


def parse_noise_handshake(text: str) -> bytes:
    _require(isinstance(text, str), "handshake frames are text")
    obj = _loads(text.encode("utf-8"))
    _require(obj["type"] == "noise/handshake", "expected noise/handshake")
    _require("=" not in str(obj["payload"].get("data", "")), "handshake data must not be padded")
    data = _b64url_decode(obj["payload"].get("data"))
    _require(0 < len(data) <= 65535, "bad handshake size")
    return data


# --- encrypted phase: framing ---------------------------------------------------------------------------------------

def json_frame(message_type: str, payload: dict) -> bytes:
    """Plaintext of an encrypted JSON message: type byte 0 + UTF-8 JSON."""
    raw = _dumps(message_type, payload).encode("utf-8")
    _require(len(raw) <= MAX_JSON_BYTES, "JSON message too large")
    return bytes([MSG_JSON]) + raw


class Defragmenter:
    """Reassembles type-1 fragments: first `[1][flags][orig_type][data]`, later `[1][flags][data]`; bit 1 of flags =
    first, bit 0 = last. Feed every decrypted plaintext; a whole plaintext comes back (type byte first) or None."""

    def __init__(self):
        self._type = None
        self._parts = []
        self._size = 0

    def feed(self, plaintext: bytes) -> bytes | None:
        _require(len(plaintext) >= 1, "empty message")
        if plaintext[0] != MSG_FRAGMENT:
            _require(self._type is None, "message inside a fragmented one")
            return plaintext
        _require(len(plaintext) >= 2, "fragment too short")
        flags, first, last = plaintext[1], bool(plaintext[1] & 2), bool(plaintext[1] & 1)
        if first:
            _require(self._type is None, "a new fragmented message started before the last ended")
            _require(len(plaintext) >= 3 and plaintext[2] != MSG_FRAGMENT, "bad first fragment")
            self._type, data = plaintext[2], plaintext[3:]
        else:
            _require(self._type is not None, "fragment without a first fragment")
            data = plaintext[2:]
        self._size += len(data)
        _require(self._size <= MAX_REASSEMBLED, "fragmented message too large")
        self._parts.append(data)
        if not last:
            return None
        whole = bytes([self._type]) + b"".join(self._parts)
        self._type, self._parts, self._size = None, [], 0
        return whole


def parse_audio_chunk(plaintext: bytes) -> AudioChunk:
    _require(len(plaintext) >= AUDIO_HEADER and plaintext[0] == MSG_AUDIO, "bad audio chunk")
    timestamp = int.from_bytes(plaintext[1:9], "big", signed=True)
    send_ahead = int.from_bytes(plaintext[9:13], "big")
    return AudioChunk(timestamp, send_ahead, bytes(plaintext[AUDIO_HEADER:]))


def build_audio_chunk(timestamp_us: int, pcm: bytes, send_ahead_us: int = 0) -> bytes:
    """The server's side of a chunk (tests)."""
    return bytes([MSG_AUDIO]) + timestamp_us.to_bytes(8, "big", signed=True) + send_ahead_us.to_bytes(4, "big") + pcm


def parse_server_message(plaintext: bytes) -> tuple:
    """Decode one whole decrypted plaintext (after Defragmenter).

    Returns ("audio", AudioChunk), ("binary", (type_byte, data)) for binary types this player does not use, or
    (message_type, validated_payload) for JSON. Unknown JSON types come back unvalidated so the caller can ignore them."""
    _require(len(plaintext) >= 1, "empty message")
    kind = plaintext[0]
    if kind == MSG_AUDIO:
        return "audio", parse_audio_chunk(plaintext)
    if kind != MSG_JSON:
        _require(kind != MSG_FRAGMENT, "unassembled fragment")
        return "binary", (kind, bytes(plaintext[1:]))
    obj = _loads(plaintext[1:])
    message_type, payload = obj["type"], obj["payload"]
    check = _SERVER_CHECKS.get(message_type)
    if check is not None:
        check(payload)
    return message_type, payload


# --- validation of the messages we act on ------------------------------------------------------------------------------

def _text(value, name: str, limit: int = MAX_NAME_BYTES) -> None:
    _require(isinstance(value, str) and len(value.encode("utf-8")) <= limit, f"{name} must be a string of at most {limit} bytes")


def _check_hello(p: dict) -> None:
    _text(p.get("name"), "name")


def _check_activate(p: dict) -> None:
    activities = p.get("activities")
    _require(isinstance(activities, list) and all(a in ACTIVITIES for a in activities), "activities invalid")
    roles = p.get("active_roles")
    _require(roles is None or (isinstance(roles, list) and all(isinstance(r, str) for r in roles)), "active_roles invalid")


def parse_flac_header(encoded) -> dict:
    """Decode and check a stream/start `codec_header`: standard padded Base64 of `fLaC` + a STREAMINFO block (42 bytes).

    Returns {"sample_rate", "channels", "bit_depth", "bytes"} where `bytes` is the header with the last-metadata-block flag
    set (libFLAC needs it to start reading audio frames after the header)."""
    _require(isinstance(encoded, str) and encoded.isascii() and len(encoded) <= 8192, "FLAC codec_header must be a Base64 string")
    _require(len(encoded) % 4 == 0, "FLAC codec_header must use padded Base64")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise SendspinProtocolError("FLAC codec_header is not valid Base64") from None
    _require(len(raw) == FLAC_HEADER_BYTES and raw[:4] == b"fLaC", "FLAC codec_header must be 'fLaC' followed by a STREAMINFO block")
    _require(raw[4] & 0x7F == 0 and int.from_bytes(raw[5:8], "big") == 34, "FLAC codec_header must hold exactly a STREAMINFO block")
    info = int.from_bytes(raw[18:26], "big")
    rate, channels, depth = info >> 44, ((info >> 41) & 7) + 1, ((info >> 36) & 0x1F) + 1
    header = raw[:4] + bytes([raw[4] | 0x80]) + raw[5:]
    return {"sample_rate": rate, "channels": channels, "bit_depth": depth, "bytes": header}


def offered_formats(available=frozenset()) -> list:
    """The formats the player announces, preferred first: FLAC (24-bit before 16-bit), then PCM, then Opus last (lossy).
    A codec whose library did not load is left out; PCM is always offered."""
    formats = []
    if "flac" in available:
        formats += [{"codec": "flac", "channels": 2, "sample_rate": rate, "bit_depth": depth} for depth in FLAC_DEPTHS for rate in PCM_RATES]
    formats += pcm_formats()
    if "opus" in available:
        formats.append({"codec": "opus", "channels": 2, "sample_rate": OPUS_RATE, "bit_depth": 16})
    return formats


def validate_stream_format(fmt) -> dict:
    """A stream/start `player` object must be one of the offered formats. Returns a normalized copy: for FLAC it also
    carries `header` (the checked codec_header bytes)."""
    _require(isinstance(fmt, dict), "stream format missing")
    codec = fmt.get("codec")
    _require(codec in CODECS, "unsupported codec")
    _require(fmt.get("channels") == 2, "only stereo is supported")
    _require(_is_int(fmt.get("sample_rate")), "sample_rate must be an integer")
    rate = fmt["sample_rate"]
    depth = fmt.get("bit_depth")
    if codec != "opus":  # `bit_depth` is meaningful for pcm and flac only, ignored (and optional) for opus
        _require(_is_int(depth), "bit_depth must be an integer")
    else:
        depth = 16
    out = {"codec": codec, "sample_rate": rate, "channels": 2, "bit_depth": depth}
    if codec == "pcm":
        _require(rate in PCM_RATES and depth == 16, "pcm is supported as 16-bit at 44100 or 48000 Hz")
    elif codec == "flac":
        _require(rate in PCM_RATES and depth in FLAC_DEPTHS, "flac is supported as 16 or 24-bit at 44100 or 48000 Hz")
        info = parse_flac_header(fmt.get("codec_header"))
        _require((info["sample_rate"], info["channels"], info["bit_depth"]) == (rate, 2, depth), "the FLAC header disagrees with the stream format")
        out["header"] = info["bytes"]
    else:
        _require(rate == OPUS_RATE, "opus is supported at 48000 Hz")
    return out


def _check_stream_start(p: dict) -> None:
    _require(_is_int(p.get("server_transmitted")), "server_transmitted missing")
    if "player" in p:
        validate_stream_format(p["player"])


def _check_stream_clear(p: dict) -> None:
    _require(_is_int(p.get("server_transmitted")), "server_transmitted missing")
    _require(p.get("from_timestamp") is None or _is_int(p["from_timestamp"]), "from_timestamp invalid")
    roles = p.get("roles")
    _require(roles is None or (isinstance(roles, list) and roles and all(r in ("player", "visualizer") for r in roles)), "roles invalid")


def _check_stream_end(p: dict) -> None:
    roles = p.get("roles")
    _require(roles is None or (isinstance(roles, list) and roles and all(r in ("player", "artwork", "visualizer") for r in roles)), "roles invalid")


def _check_server_time(p: dict) -> None:
    for key in ("client_transmitted", "server_received", "server_transmitted"):
        _require(_is_int(p.get(key)), f"{key} missing")


def _check_group_update(p: dict) -> None:
    _require(p.get("playback_state") in (None, "playing", "stopped"), "playback_state invalid")


def _check_command(p: dict) -> None:
    player = p.get("player")
    if player is None:
        return  # commands for roles we do not have
    _require(isinstance(player, dict), "player command must be an object")
    command = player.get("command")
    _require(command in PLAYER_COMMANDS, "unknown player command")
    if command == "volume":
        _require(_is_int(player.get("volume")) and 0 <= player["volume"] <= 100, "volume must be 0-100")
    elif command == "mute":
        _require(isinstance(player.get("mute"), bool), "mute must be true or false")
    else:
        _require(_is_int(player.get("output_delay_ms")) and 0 <= player["output_delay_ms"] <= 5000, "output_delay_ms must be 0-5000")


_SERVER_CHECKS = {
    "server/hello": _check_hello,
    "server/activate": _check_activate,
    "server/command": _check_command,
    "server/time": _check_server_time,
    "stream/start": _check_stream_start,
    "stream/clear": _check_stream_clear,
    "stream/end": _check_stream_end,
    "group/update": _check_group_update,
}


# --- what we send ----------------------------------------------------------------------------------------------------------

def pcm_formats() -> list:
    return [{"codec": "pcm", "channels": 2, "sample_rate": rate, "bit_depth": 16} for rate in PCM_RATES]


def build_client_hello(name: str, buffer_capacity: int, unpaired_access: bool = True, device_info: dict | None = None,
                       available=frozenset()) -> bytes:
    """Plaintext of the encrypted `client/hello`: the player role only (D14); `available` names the codecs that can be decoded
    ("flac", "opus"), PCM is always offered."""
    _text(name, "name")
    _require(_is_int(buffer_capacity) and buffer_capacity > 0, "buffer_capacity must be positive")
    payload = {"name": name}
    if device_info:
        _require(all(isinstance(v, str) and len(v.encode()) <= MAX_NAME_BYTES for v in device_info.values()), "device_info values too long")
        payload["device_info"] = dict(device_info)
    payload.update({
        "supported_roles": ["player@v1"],
        "player@v1_support": {"supported_formats": offered_formats(available), "buffer_capacity": buffer_capacity},
        "supported_pair_methods": {"pairing_psk": {}},
        "unpaired_access": {"enabled": bool(unpaired_access)},
    })
    return json_frame("client/hello", payload)


def build_client_state(available: bool, volume: int, muted: bool, output_delay_ms: int, required_lead_time_ms: int = 0,
                       min_buffer_ms: int = 0, fmt: dict | None = None) -> bytes:
    """Plaintext of `client/state` for the player role. `available` stays False until the clock has converged."""
    _require(0 <= volume <= 100 and 0 <= output_delay_ms <= 5000 and required_lead_time_ms >= 0 and 0 <= min_buffer_ms <= 5000,
             "state value out of range")
    player = {"volume": volume, "muted": bool(muted), "output_delay_ms": output_delay_ms, "required_lead_time_ms": required_lead_time_ms,
              "min_buffer_ms": min_buffer_ms, "supported_commands": list(PLAYER_COMMANDS)}
    if fmt is not None:
        checked = validate_stream_format(fmt)
        player["format"] = {k: checked[k] for k in ("codec", "sample_rate", "channels", "bit_depth")}
    return json_frame("client/state", {"available": bool(available), "player": player})


def build_client_time(client_transmitted_us: int) -> bytes:
    return json_frame("client/time", {"client_transmitted": client_transmitted_us})


def build_client_goodbye(reason: str) -> bytes:
    _require(reason in GOODBYE_REASONS, "unknown goodbye reason")
    return json_frame("client/goodbye", {"reason": reason})
