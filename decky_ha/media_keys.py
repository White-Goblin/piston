"""Play/pause/next/previous for the running app: MPRIS when a player is on the session bus, otherwise a
virtual media key through /dev/uinput."""
import asyncio
import fcntl
import os
import struct
import subprocess
import time

import decky

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
EV_KEY, EV_SYN = 1, 0


class VirtualKeyboard:
    """A uinput keyboard that can press the media keys. Created on first use and kept."""

    def __init__(self, path: str = UINPUT_PATH):
        self._fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self._fd, UI_SET_EVBIT, EV_KEY)
        for code in KEY_CODES.values():
            fcntl.ioctl(self._fd, UI_SET_KEYBIT, code)
        os.write(self._fd, struct.pack("80sHHHHi" + "i" * 256, b"decky-home-assistant", 3, 1, 1, 1, 0, *([0] * 256)))
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

    def _mpris_players(self) -> list:
        """Bus names of MPRIS players, the playing ones first."""
        try:
            listing = self._busctl("list", "--no-legend")
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return []
        names = [line.split()[0] for line in listing.splitlines() if line.startswith(MPRIS_PREFIX)]

        def rank(name: str) -> int:
            try:
                status = self._busctl("get-property", name, MPRIS_PATH, MPRIS_PLAYER, "PlaybackStatus")
            except (RuntimeError, OSError, subprocess.SubprocessError):
                return 3
            return 0 if '"Playing"' in status else 1 if '"Paused"' in status else 2

        return sorted(names, key=rank)

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
        return await asyncio.get_running_loop().run_in_executor(None, self._send_media_key, key)

    def _close_keyboard(self) -> None:
        if self.keyboard is not None:
            try:
                self.keyboard.close()
            except OSError:
                pass
            self.keyboard = None
