"""Sendspin artwork channel: put the announce/part messages (binary type 8) back together into one current cover.

Framing (spec, verified with Music Assistant, swe/notes/sendspin/announcement-wire.md § 2):
  announce  [8][flags 0x02][timestamp int64 us][total_size uint32]   total_size 0 clears the image
  part      [8][flags 0x00][data]                                   at most 65519 bytes
  cancel    [8][flags 0x01]
One transfer is in flight at a time. A finished image becomes current when its (clock converted) timestamp is reached; an image
that arrives late is still shown. A transfer that breaks the rules is discarded and the previous image stays."""
import hashlib
import struct
from typing import NamedTuple

import decky

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_PART_BYTES = 65517  # a Noise message holds 65519 bytes of plaintext: type and flags take 2
FLAG_CANCEL, FLAG_ANNOUNCE = 0x01, 0x02
ANNOUNCE_LEN = 14  # type + flags + int64 + uint32
ARTWORK_TYPE = 8


class Cover(NamedTuple):
    id: str
    content_type: str
    data: bytes


def sniff_content_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "application/octet-stream"


class ArtworkAssembler:
    """`to_local(server_us)` converts a server timestamp to the local clock (None while the clock is not usable: the image is
    then shown at once); `now()` is the local time in microseconds."""

    def __init__(self, to_local, now):
        self._to_local = to_local
        self._now = now
        self._buffer = None       # bytearray of the transfer in flight
        self._expected = 0
        self._timestamp = 0
        self._pending = None      # (timestamp_us, bytes): complete, waiting for its time
        self._pending_clear = None  # timestamp_us of a clear that is waiting for its time
        self._current = None
        self._counter = 0
        self.discarded = 0

    # --- input ---------------------------------------------------------------------------------------------------------

    def feed(self, plaintext: bytes) -> list:
        """One whole type-8 plaintext. Returns events: "discard", "complete", "clear", "cancel", and "changed" when the
        current cover changed now."""
        events = []
        if len(plaintext) < 2 or plaintext[0] != ARTWORK_TYPE:
            return self._discard(events, "malformed message")
        flags = plaintext[1]
        if flags & FLAG_ANNOUNCE:
            if len(plaintext) != ANNOUNCE_LEN:
                return self._discard(events, "bad announce length")
            timestamp, size = struct.unpack(">qI", plaintext[2:])
            self._buffer = None  # a new announce cancels one in flight
            if size == 0:
                self._pending = None
                self._pending_clear = timestamp
                events.append("clear")
            elif size > MAX_IMAGE_BYTES:
                return self._discard(events, f"image of {size} bytes is over the cap")
            else:
                self._buffer, self._expected, self._timestamp = bytearray(), size, timestamp
        elif flags & FLAG_CANCEL:
            if self._buffer is not None:
                self._buffer = None
                events.append("cancel")
        else:
            data = plaintext[2:]
            if self._buffer is None:
                return self._discard(events, "part without an announce")
            if len(data) > MAX_PART_BYTES or len(self._buffer) + len(data) > self._expected:
                return self._discard(events, "more bytes than announced")
            self._buffer += data
            if len(self._buffer) == self._expected:
                self._pending = (self._timestamp, bytes(self._buffer))
                self._pending_clear = None
                self._buffer = None
                events.append("complete")
        if self.poll():
            events.append("changed")
        return events

    def _discard(self, events: list, why: str) -> list:
        self._buffer = None
        self.discarded += 1
        decky.logger.debug(f"Sendspin artwork transfer discarded: {why}")
        events.append("discard")
        return events

    # --- time ----------------------------------------------------------------------------------------------------------

    def _due(self, timestamp_us: int) -> bool:
        try:
            local = self._to_local(timestamp_us)
        except Exception:
            local = None
        return local is None or local <= self._now()

    def poll(self) -> bool:
        """Apply a waiting image or clear whose time has come. True when the current cover changed."""
        if self._pending is not None and self._due(self._pending[0]):
            data = self._pending[1]
            self._pending = None
            self._counter += 1
            digest = hashlib.sha256(data).hexdigest()[:12]
            self._current = Cover(f"{self._counter}-{digest}", sniff_content_type(data), data)
            return True
        if self._pending_clear is not None and self._due(self._pending_clear):
            self._pending_clear = None
            changed = self._current is not None
            self._current = None
            return changed
        return False

    def waiting(self) -> bool:
        return self._pending is not None or self._pending_clear is not None

    # --- output --------------------------------------------------------------------------------------------------------

    def current(self) -> Cover | None:
        return self._current

    def lookup(self, cover_id: str) -> Cover | None:
        current = self._current
        return current if current is not None and current.id == cover_id else None

    def reset(self) -> None:
        """Forget everything (the session ended)."""
        self._buffer = self._pending = self._pending_clear = self._current = None
