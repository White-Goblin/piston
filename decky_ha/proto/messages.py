"""Frame shapes, validation and version negotiation for the LAN protocol (stdlib only)."""
import json

import re

from . import MIN_PROTOCOL_VERSION, PROTOCOL_VERSION

MAX_FRAME_BYTES = 1_000_000
MAX_NAME_LEN = 64
MAX_BOOT_ID_LEN = 32
MEDIA_GROUP = "media"  # state group: now-playing details (protocol v3)
CMD_GET_ARTWORK = "get_artwork"  # command: one chunk of the current artwork (protocol v3)
ARTWORK_CHUNK_BYTES = 384 * 1024  # raw bytes per get_artwork result (about 512 KiB once base64 encoded)
MAX_ARTWORK_BYTES = 2 * 1024 * 1024  # larger images are not offered at all
MAX_ART_ID_LEN = 64

# Error codes carried in `result.error.code` (and used as exception codes).
BAD_REQUEST = "bad_request"
UNAUTHORIZED = "unauthorized"
BUSY = "busy"
OFFLINE = "offline"
NOT_RUNNING = "not_running"
UNSUPPORTED = "unsupported"
INTERNAL = "internal"
VERSION_SKEW = "version_skew"
ERROR_CODES = (BAD_REQUEST, UNAUTHORIZED, BUSY, OFFLINE, NOT_RUNNING, UNSUPPORTED, INTERNAL, VERSION_SKEW)

FRAME_TYPES = ("hello", "challenge", "proof", "pair", "cmd", "result", "state", "event", "update", "update_status")

# Update lane frames (see docs/protocol.md): HA tells the device which release to install, the device reports progress.
UPDATE_STATES = ("idle", "downloading", "waiting_for_ui", "installing", "failed")
UPDATE_REASONS = ("bad_request", "busy", "download", "hash", "archive", "installer", "version")
MAX_UPDATE_URL_LEN = 512
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProtocolError(Exception):
    """A frame or exchange violated the protocol. `code` is one of ERROR_CODES."""

    def __init__(self, code: str, message: str = "", data: dict | None = None):
        super().__init__(message or code)
        self.code, self.message, self.data = code, message or code, data or {}


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ProtocolError(BAD_REQUEST, message)


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def hello(device_id: str, proto: int = PROTOCOL_VERSION, min_proto: int = MIN_PROTOCOL_VERSION) -> dict:
    return {"type": "hello", "proto": proto, "min_proto": min_proto, "device_id": device_id}


def device_hello_extra(plugin_version: str, lane: int) -> dict:
    """Fields the device adds to its hello (frozen with the update lane)."""
    return {"plugin_version": plugin_version, "lane": lane}


def update(version: str, url: str, sha256: str) -> dict:
    return {"type": "update", "version": version, "url": url, "sha256": sha256}


def update_status(state: str, version: str = "", reason: str | None = None) -> dict:
    frame = {"type": "update_status", "state": state, "version": version}
    if reason is not None:
        frame["reason"] = reason
    return frame


def cmd(msg_id: int, name: str, data: dict | None = None) -> dict:
    return {"type": "cmd", "id": msg_id, "name": name, "data": data or {}}


def result_ok(msg_id: int, data: dict | None = None) -> dict:
    return {"type": "result", "id": msg_id, "ok": True, "data": data or {}}


def result_error(msg_id: int, code: str, message: str = "") -> dict:
    return {"type": "result", "id": msg_id, "ok": False, "error": {"code": code, "message": message or code}}


def state(name: str, data: dict, boot_id: str, seq: int) -> dict:
    """A state group value. `boot_id` identifies the device backend run, `seq` counts pushes of this group (from 1)."""
    return {"type": "state", "name": name, "data": data, "boot_id": boot_id, "seq": seq}


def event(name: str, data: dict | None = None) -> dict:
    return {"type": "event", "name": name, "data": data or {}}


def artwork_request(data) -> tuple:
    """Validate `get_artwork` data and return (art id, offset); raises ProtocolError(bad_request)."""
    _require(isinstance(data, dict), "data must be an object")
    art_id, offset = data.get("id"), data.get("offset", 0)
    _require(isinstance(art_id, str) and 0 < len(art_id) <= MAX_ART_ID_LEN, "id must be a non-empty string")
    _require(isinstance(offset, int) and not isinstance(offset, bool) and 0 <= offset <= MAX_ARTWORK_BYTES,
             "offset must be an integer between 0 and the artwork size limit")
    return art_id, offset


def encode(frame: dict) -> str:
    text = json.dumps(frame, separators=(",", ":"))
    if len(text.encode()) > MAX_FRAME_BYTES:
        raise ProtocolError(BAD_REQUEST, "frame too large")
    return text


def parse(text) -> dict:
    """Decode and validate one frame; raises ProtocolError(bad_request) on anything malformed."""
    raw = text.encode() if isinstance(text, str) else bytes(text)
    _require(len(raw) <= MAX_FRAME_BYTES, "frame too large")
    try:
        frame = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ProtocolError(BAD_REQUEST, "invalid JSON") from None
    _require(isinstance(frame, dict), "frame must be an object")
    ftype = frame.get("type")
    _require(ftype in FRAME_TYPES, "unknown frame type")
    if ftype == "hello":
        for key in ("proto", "min_proto"):
            _require(isinstance(frame.get(key), int) and not isinstance(frame.get(key), bool) and frame[key] >= 0,
                     f"hello.{key} must be a non-negative integer")
        _require(isinstance(frame.get("device_id"), str) and frame["device_id"], "hello.device_id required")
        if "plugin_version" in frame:
            _require(isinstance(frame["plugin_version"], str) and VERSION_RE.match(frame["plugin_version"]),
                     "hello.plugin_version must look like 1.2.3")
        if "lane" in frame:
            _require(isinstance(frame["lane"], int) and not isinstance(frame["lane"], bool) and frame["lane"] >= 0,
                     "hello.lane must be a non-negative integer")
    elif ftype == "update":
        _require(isinstance(frame.get("version"), str) and VERSION_RE.match(frame["version"]), "update.version must look like 1.2.3")
        url = frame.get("url")
        _require(isinstance(url, str) and 0 < len(url) <= MAX_UPDATE_URL_LEN, "update.url invalid")
        _require(isinstance(frame.get("sha256"), str) and SHA256_RE.match(frame["sha256"]), "update.sha256 must be 64 lowercase hex digits")
    elif ftype == "update_status":
        _require(frame.get("state") in UPDATE_STATES, "update_status.state invalid")
        version = frame.get("version", "")
        _require(isinstance(version, str) and (version == "" or VERSION_RE.match(version)), "update_status.version invalid")
        frame.setdefault("version", "")
        if "reason" in frame:
            _require(frame["reason"] in UPDATE_REASONS, "update_status.reason invalid")
    elif ftype == "cmd":
        _require(_is_id(frame.get("id")), "cmd.id must be a non-negative integer")
        _require(_is_name(frame.get("name")), "cmd.name invalid")
        _require(isinstance(frame.get("data", {}), dict), "cmd.data must be an object")
        frame.setdefault("data", {})
    elif ftype == "result":
        _require(_is_id(frame.get("id")), "result.id must be a non-negative integer")
        _require(isinstance(frame.get("ok"), bool), "result.ok must be a boolean")
        if not frame["ok"]:
            err = frame.get("error")
            _require(isinstance(err, dict) and isinstance(err.get("code"), str), "result.error.code required")
        else:
            _require(isinstance(frame.get("data", {}), dict), "result.data must be an object")
            frame.setdefault("data", {})
    elif ftype in ("state", "event"):
        _require(_is_name(frame.get("name")), f"{ftype}.name invalid")
        _require(isinstance(frame.get("data", {}), dict), f"{ftype}.data must be an object")
        frame.setdefault("data", {})
        if ftype == "state":
            boot_id = frame.get("boot_id")
            _require(isinstance(boot_id, str) and 0 < len(boot_id) <= MAX_BOOT_ID_LEN, "state.boot_id invalid")
            _require(_is_id(frame.get("seq")) and frame["seq"] >= 1, "state.seq must be an integer >= 1")
    return frame


def _is_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_name(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_NAME_LEN


def negotiate(own: int, own_min: int, peer: int, peer_min: int) -> int:
    """Return the protocol version both sides speak, or raise ProtocolError(version_skew).

    Each side accepts the versions from its minimum to its own; the ranges must overlap
    (with both minimums equal to their own version, only an identical version works).
    `data["peer"]` is "old" when the other side is too old (update it) or "new" when it is too new.
    """
    if peer < own_min:
        raise ProtocolError(VERSION_SKEW, "the other side is too old", {"peer": "old"})
    if peer_min > own:
        raise ProtocolError(VERSION_SKEW, "the other side is too new", {"peer": "new"})
    return min(own, peer)
