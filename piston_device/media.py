"""The `media` state group: what the chosen MPRIS player (or the running app, from Steam's art) is playing.

Called from the playback poll in media_keys.py. Live state only: nothing is stored, and the group is pushed only when it
changes. Image bytes never go into the group, only an id; they are served by get_artwork (see artwork.py)."""
import asyncio
import base64
import os
import time

import decky

from . import artwork, media_model
from .proto import messages as m

POSITION_TOLERANCE = 2.0  # seconds; a position that is within this of where it should be is not pushed again
FAILURES_BEFORE_CLEAR = 2  # one failed poll keeps the last group, a second one in a row clears it
ART_ATTEMPTS = 3  # tries (one per poll) to load art that is not there yet, per track


class MediaMixin:
    def _init_media(self) -> None:
        self.art_store = artwork.ArtStore()
        self._media_sent = None  # last pushed group (None = nothing pushed yet)
        self._media_status = ""
        self._media_failures = 0
        self._art_key = None
        self._art_attempts = 0

    def _steam_root(self) -> str:
        home = getattr(decky, "DECKY_USER_HOME", None) or os.path.expanduser("~")
        return os.path.join(home, ".local", "share", "Steam")

    def _art_loader(self, key: tuple, meta: dict):
        if key[0] == "mpris":
            return lambda: artwork.load_from_url(meta["art_url"])
        return lambda: artwork.load_steam(key[1], self._steam_root())

    async def _load_media_art(self, chosen, app):
        """The id of the image for the current media (loaded only when the track changes), or None."""
        meta = (chosen or {}).get("meta") or {}
        if chosen and meta.get("art_url"):
            key = ("mpris", meta.get("trackid"), meta["art_url"])
        elif not chosen and app:
            key = ("steam", app["appid"])
        else:
            key = None
        if key is None:
            self._art_key, self._art_attempts = None, 0
            self.art_store.set(None)
            return None
        if key == self._art_key and (self.art_store.art is not None or self._art_attempts >= ART_ATTEMPTS):
            return self.art_store.id
        if key != self._art_key:
            self._art_key, self._art_attempts = key, 0
        self._art_attempts += 1
        art = await asyncio.get_running_loop().run_in_executor(None, self._art_loader(key, meta))
        self.art_store.set(art)
        return self.art_store.id

    def _stabilize(self, media: dict, status: str) -> dict:
        """Keep the previous position while it is where playback should have taken it (HA extrapolates), so the group is
        pushed again only for a seek, a restart or a play/pause change."""
        last = self._media_sent
        if last and "position" in media and "position" in last and status == self._media_status:
            advanced = (media["position_at"] - last["position_at"]) if status == "Playing" else 0.0
            if abs(media["position"] - (last["position"] + advanced)) <= POSITION_TOLERANCE:
                return {**media, "position": last["position"], "position_at": last["position_at"]}
        return media

    async def _update_media(self, players) -> None:
        try:
            await self._update_media_inner(players)
        except Exception as e:  # noqa: BLE001 - a failed media read must not end the playback loop
            decky.logger.warning(f"Media update failed: {e}")

    async def _update_media_inner(self, players) -> None:
        app = media_model.app_info(self.app_state, self._offered_apps())
        chosen = None
        failed = players is None
        if not failed:
            chosen = media_model.choose_player(players, app)
            if chosen:
                try:
                    meta, position = await asyncio.get_running_loop().run_in_executor(None, self._read_details, chosen["name"])
                except Exception as e:  # noqa: BLE001 - the player went away or the bus call failed
                    decky.logger.debug(f"Could not read {chosen['name']}: {e}")
                    failed = True
                else:
                    chosen = {**chosen, "meta": meta, "position": position}
        if failed:
            self._media_failures += 1
            if self._media_failures < FAILURES_BEFORE_CLEAR:
                return
            chosen = None
        else:
            self._media_failures = 0
        art_id = await self._load_media_art(chosen, app)
        media = media_model.build_media(chosen, app, art_id, now=time.time())
        status = chosen["status"] if chosen else ""
        media = self._stabilize(media, status)
        self._media_status = status
        if media == self._media_sent or (self._media_sent is None and not media):
            return
        self._media_sent = media
        self._push("media", media)

    async def _cmd_get_artwork(self, data: dict) -> dict:
        """One chunk of the current image: {content_type, size, offset, data (base64), done}."""
        art_id, offset = m.artwork_request(data)
        try:
            content_type, size, piece, done = self.art_store.chunk(art_id, offset)
        except LookupError:
            raise m.ProtocolError(m.NOT_RUNNING, "that artwork is not current any more") from None
        except ValueError:
            raise m.ProtocolError(m.BAD_REQUEST, "offset is past the end of the image") from None
        return {"content_type": content_type, "size": size, "offset": offset,
                "data": base64.b64encode(piece).decode(), "done": done}
