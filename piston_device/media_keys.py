"""Play/pause/next/previous for the running app: MPRIS when a player is on the session bus, otherwise a
virtual media key through /dev/uinput."""
import asyncio
import fcntl
import math
import os
import struct
import subprocess
import time

import decky

from . import media_model, mpris_meta
from .proto import messages as m

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"
MPRIS_METHODS = {"play_pause": "PlayPause", "play": "Play", "pause": "Pause", "next": "Next",
                 "previous": "Previous", "stop": "Stop"}
# linux/input-event-codes.h
KEY_CODES = {"play_pause": 164, "play": 200, "pause": 201, "next": 163, "previous": 165, "stop": 166}

UI_SET_EVBIT, UI_SET_KEYBIT, UI_DEV_CREATE, UI_DEV_DESTROY = 0x40045564, 0x40045565, 0x5501, 0x5502
UINPUT_PATH = "/dev/uinput"
PLAYBACK_POLL_SECONDS = 2
SEEK_LIMIT_SECONDS = 3600
SEEK_POSITION_LIMIT_SECONDS = 7 * 24 * 3600
EV_KEY, EV_SYN = 1, 0


class VirtualKeyboard:
    """A uinput keyboard that can press the media keys. Created on first use and kept."""

    def __init__(self, path: str = UINPUT_PATH):
        self._fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_KEY)
        for code in KEY_CODES.values():
            fcntl.ioctl(self._fd, UI_SET_KEYBIT, code)
        os.write(self._fd, struct.pack("80sHHHHi" + "i" * 256, b"piston", 3, 1, 1, 1, 0, *([0] * 256)))
        fcntl.ioctl(self._fd, UI_DEV_CREATE)
        time.sleep(0.5)  # let the system notice the new device before the first key

    def _event(self, etype: int, code: int, value: int) -> None:
        os.write(self._fd, struct.pack("llHHi", 0, 0, etype, code, value))

    def press(self, key: str) -> None:
        code = KEY_CODES[key]
        self._event(EV_KEY, code, 1)
        self._event(EV_SYN, 0, 0)
        time.sleep(0.05)
        self._event(EV_KEY, code, 0)
        self._event(EV_SYN, 0, 0)

    def close(self) -> None:
        try:
            fcntl.ioctl(self._fd, UI_DEV_DESTROY)
        finally:
            os.close(self._fd)


class MediaKeyMixin:
    keyboard: VirtualKeyboard | None = None

    def _read_players(self):
        """Blocking. [{"name", "status": "Playing" | "Paused" | "Stopped" | "", "meta": {}, "position": None}] for every MPRIS
        player on the session bus, or None when the bus could not be listed."""
        try:
            listing = self._busctl("list", "--no-legend")
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return None
        found = []
        for line in listing.splitlines():
            if not line.startswith(MPRIS_PREFIX):
                continue
            name = line.split()[0]
            try:
                raw = self._busctl("get-property", name, MPRIS_PATH, MPRIS_PLAYER, "PlaybackStatus")
            except (RuntimeError, OSError, subprocess.SubprocessError):
                raw = ""
            status = next((st for st in ("Playing", "Paused", "Stopped") if f'"{st}"' in raw), "")
            found.append({"name": name, "status": status, "meta": {}, "position": None})
        return found

    def _read_details(self, name: str) -> tuple:
        """Blocking. (metadata, position in seconds | None) of one player; raises when the bus call fails."""
        meta = mpris_meta.parse_metadata(self._busctl("get-property", name, MPRIS_PATH, MPRIS_PLAYER, "Metadata"))
        try:
            position = mpris_meta.parse_position(self._busctl("get-property", name, MPRIS_PATH, MPRIS_PLAYER, "Position"))
        except (RuntimeError, OSError, subprocess.SubprocessError):
            position = None  # some players have no position
        return meta, position

    def _mpris_statuses(self) -> list:
        """[(bus name, "Playing" | "Paused" | "Stopped" | "")] for every MPRIS player on the session bus."""
        return [(p["name"], p["status"]) for p in self._read_players() or []]

    def _mpris_players(self) -> list:
        """Bus names of MPRIS players, the playing ones first."""
        order = {"Playing": 0, "Paused": 1, "Stopped": 2}
        return [name for name, status in sorted(self._mpris_statuses(), key=lambda p: order.get(p[1], 3))]

    def _playback_status(self) -> str:
        """"playing", "paused", "stopped" or "unknown" (no player reports anything, for example a game)."""
        statuses = {status for _, status in self._mpris_statuses()}
        for status in ("Playing", "Paused", "Stopped"):
            if status in statuses:
                return status.lower()
        return "unknown"

    def _offered_apps(self) -> list:
        return self._load_settings()["apps"]

    async def _update_media(self, players) -> None:
        """Hook: the media group follows the players (see media.py)."""

    async def _refresh_playback(self) -> None:
        """Update the apps group when the playback status changed, then the media group from the same read."""
        players = await asyncio.get_running_loop().run_in_executor(None, self._read_players)
        listed = players or []
        status = "unknown"
        if self.app_state["running"]:
            statuses = {p["status"] for p in listed}  # nothing is playing without a running app
            status = next((st.lower() for st in ("Playing", "Paused", "Stopped") if st in statuses), "unknown")
        else:  # no Steam app (desktop mode, or Steam reports none): a Playing or Paused player is still the media
            chosen = media_model.choose_player(listed, None)
            status = chosen["status"].lower() if chosen else "unknown"
        if status != self.app_state.get("playback"):
            self._set_app_state(playback=status)
        await self._update_media(players)

    async def _playback_loop(self) -> None:
        while True:
            try:
                await self._refresh_playback()
            except Exception as e:  # noqa: BLE001 - a failed poll must not end the loop
                decky.logger.warning(f"Playback status failed: {e}")
            await asyncio.sleep(PLAYBACK_POLL_SECONDS)

    def _press_virtual_key(self, key: str) -> None:
        if self.keyboard is None:
            self.keyboard = VirtualKeyboard()
        self.keyboard.press(key)

    def _send_media_key(self, key: str) -> dict:
        """Blocking. Raises ProtocolError(not_running | unsupported | internal)."""
        for player in self._mpris_players():
            try:
                self._busctl("call", player, MPRIS_PATH, MPRIS_PLAYER, MPRIS_METHODS[key])
                return {"via": "mpris", "player": player[len(MPRIS_PREFIX):]}
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                decky.logger.warning(f"MPRIS {key} on {player} failed: {e}")
        if not self.app_state["running"]:
            raise m.ProtocolError(m.NOT_RUNNING, "nothing is playing")
        try:
            self._press_virtual_key(key)
        except OSError as e:
            raise m.ProtocolError(m.UNSUPPORTED, f"could not send a media key: {e}") from None
        return {"via": "uinput"}

    async def _cmd_media_key(self, data: dict) -> dict:
        key = data.get("key")
        if key not in MPRIS_METHODS:
            raise m.ProtocolError(m.BAD_REQUEST, f"key must be one of {', '.join(MPRIS_METHODS)}")
        result = await asyncio.get_running_loop().run_in_executor(None, self._send_media_key, key)
        await asyncio.sleep(0.4)  # let the player change state, so Home Assistant shows play/pause at once
        await self._refresh_playback()
        return result

    def _send_media_seek(self, offset, position) -> dict:
        """Blocking. Moves the first MPRIS player that accepts it (playing ones first) by `offset` seconds or to `position`
        seconds. Raises ProtocolError(not_running | unsupported). The absolute `SetPosition` is preferred: some players (YouTube
        in a Chromium shell) pause and report a stale position after a relative `Seek`, and honor `SetPosition` properly. A
        player without a track id, or that rejects it, gets the relative `Seek`. There is no virtual-key fallback: apps ignore
        those keys."""
        order = {"Playing": 0, "Paused": 1, "Stopped": 2}
        players = sorted(self._mpris_statuses(), key=lambda p: order.get(p[1], 3))
        if not players and not self.app_state["running"]:
            raise m.ProtocolError(m.NOT_RUNNING, "nothing is playing")
        for name, _status in players:
            try:
                meta, now = self._read_details(name)
            except (RuntimeError, OSError, subprocess.SubprocessError):
                meta, now = {}, None
            target = position if position is not None else (now + offset if now is not None else None)
            trackid = meta.get("trackid")
            if trackid and target is not None:
                target = max(0.0, min(target, meta["length"]) if meta.get("length") else target)
                try:
                    self._busctl("call", name, MPRIS_PATH, MPRIS_PLAYER, "SetPosition", "ox", trackid, str(round(target * 1_000_000)))
                    return {"via": "mpris", "player": name[len(MPRIS_PREFIX):]}
                except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                    decky.logger.warning(f"MPRIS SetPosition on {name} failed: {e}")
            relative = offset if offset is not None else (position - now if now is not None else None)
            if relative is None:
                continue
            try:
                # `--` first: busctl would read a negative offset as an option
                self._busctl("call", name, MPRIS_PATH, MPRIS_PLAYER, "Seek", "x", "--", str(round(relative * 1_000_000)))
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                decky.logger.warning(f"MPRIS Seek on {name} failed: {e}")
                continue
            return {"via": "mpris", "player": name[len(MPRIS_PREFIX):]}
        raise m.ProtocolError(m.UNSUPPORTED, "no player accepted the seek")

    async def _cmd_media_seek(self, data: dict) -> dict:
        offset, position = data.get("offset"), data.get("position")

        def number(value, low, high):
            return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and low <= value <= high

        if (offset is None) == (position is None) or (offset is not None and not number(offset, -SEEK_LIMIT_SECONDS, SEEK_LIMIT_SECONDS)) \
                or (position is not None and not number(position, 0, SEEK_POSITION_LIMIT_SECONDS)):
            raise m.ProtocolError(m.BAD_REQUEST, f"give either offset (seconds, within {SEEK_LIMIT_SECONDS}) or position (seconds from the start)")
        result = await asyncio.get_running_loop().run_in_executor(None, self._send_media_seek, offset, position)
        await self._refresh_playback()
        return result

    def _close_keyboard(self) -> None:
        if self.keyboard is not None:
            try:
                self.keyboard.close()
            except OSError:
                pass
            self.keyboard = None
