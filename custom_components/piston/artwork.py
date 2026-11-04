"""Fetch the current media image from the device in chunks and keep it (one image, keyed by its id)."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
from typing import Awaitable, Callable

from .client import CommandError, DeviceOffline
from .proto import messages as m

_LOGGER = logging.getLogger(__name__)

MAX_CHUNKS = 64  # a defence against a device that never says "done" (the cap is 2 MiB in 384 KiB chunks)
RESTARTS = 2  # how often a fetch starts over because the image changed while it was being read


class ArtworkFetcher:
    """`get()` returns (bytes, content type) of the image the media group currently names, or (None, None).

    Only the current image is kept. Simultaneous calls share one fetch. When the device says the id is no longer current
    (the track changed mid-fetch) the fetch starts again with the new id; an id whose bytes do not hash to it is rejected."""

    def __init__(self, command: Callable[[str, dict], Awaitable[dict]], current_id: Callable[[], str | None]):
        self._command = command
        self._current_id = current_id
        self._cached: tuple[str, bytes, str] | None = None
        self._inflight: tuple[str, asyncio.Task] | None = None

    async def get(self) -> tuple:
        wanted = self._current_id()
        if not wanted:
            return None, None
        if self._cached and self._cached[0] == wanted:
            return self._cached[1], self._cached[2]
        if self._inflight is None or self._inflight[0] != wanted or self._inflight[1].done():
            self._inflight = (wanted, asyncio.get_running_loop().create_task(self._fetch(wanted)))
        task = self._inflight[1]
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never let an image problem become an error in the entity
            _LOGGER.debug("could not fetch the media image: %s", e)
            return None, None

    async def _fetch(self, wanted: str) -> tuple:
        restarts = 0
        while True:
            chunks, offset, content_type, done = [], 0, "", False
            try:
                for _ in range(MAX_CHUNKS):
                    result = await self._command(m.CMD_GET_ARTWORK, {"id": wanted, "offset": offset})
                    piece = base64.b64decode(result["data"], validate=True)
                    chunks.append(piece)
                    content_type = result.get("content_type") or content_type
                    offset += len(piece)
                    if result.get("done"):
                        done = True
                        break
                    if not piece:
                        break
            except CommandError as e:
                current = self._current_id()
                if e.code == m.NOT_RUNNING and current and current != wanted and restarts < RESTARTS:
                    wanted, restarts = current, restarts + 1  # the track changed while we were reading
                    continue
                _LOGGER.debug("the device has no media image %s: %s", wanted, e)
                return None, None
            except (DeviceOffline, KeyError, TypeError, ValueError, binascii.Error) as e:
                _LOGGER.debug("could not read the media image: %s", e)
                return None, None
            data = b"".join(chunks)
            if not done or hashlib.sha256(data).hexdigest()[:16] != wanted:
                _LOGGER.debug("the media image %s arrived incomplete or damaged", wanted)
                return None, None
            self._cached = (wanted, data, content_type or "image/jpeg")
            return data, self._cached[2]
