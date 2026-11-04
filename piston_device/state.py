"""State groups pushed to the integration (see docs/protocol.md)."""
import asyncio
import os
import time

import decky

from .lifecycle import Lifecycle
from .proto import PROTOCOL_VERSION
from .wol import wol_info


DEVICE_REFRESH_SECONDS = 60
SESSION_CHECK_SECONDS = 5
RESUME_CHECK_SECONDS = 2
RESUME_JUMP_SECONDS = 4  # the wall clock runs this much ahead of the monotonic clock after a suspend
GAME_PROCESSES = ("gamescope", "gamescope-wl")
DESKTOP_PROCESSES = ("plasmashell", "kwin_wayland", "kwin_x11")


def detect_session(comms=None) -> str:
    """"game" (the Steam gaming session), "desktop" (the Plasma session) or "unknown", from running process names.

    The plugin backend is a system service, so it keeps running when the device switches between the two."""
    if comms is None:
        comms = set()
        try:
            for pid in os.listdir("/proc"):
                if pid.isdigit():
                    try:
                        with open(f"/proc/{pid}/comm") as f:
                            comms.add(f.read().strip())
                    except OSError:
                        pass
        except OSError:
            return "unknown"
    if comms & set(GAME_PROCESSES):
        return "game"
    if comms & set(DESKTOP_PROCESSES):
        return "desktop"
    return "unknown"


class StateMixin:
    """`self.groups` holds the latest value of every state group; `_push` updates it and, when the
    integration is connected, sends it. A new connection gets `_snapshot()` first."""

    def _init_state(self) -> None:
        self.groups: dict = {}
        self.server = None
        self._held: dict | None = None  # group names (in first-push order) whose pushes are held back, see _hold_pushes
        self._app_synced = asyncio.Event()  # set whenever the frontend reports the running app (see resume.py)
        self.lifecycle: Lifecycle | None = None
        self.session = "unknown"
        self._wol_cache: dict = {"interface": "", "error": "not read yet"}

    def _push(self, name: str, data: dict) -> None:
        self.groups[name] = data
        if self._held is not None:
            self._held[name] = None  # collected; sent by _release_pushes if the value ended up different
            return
        if self.server is not None:
            self.server.push_state(name, data)

    def _hold_pushes(self) -> dict:
        """Collect state pushes instead of sending them (the resume refresh sends only what changed, after the network is up).
        Returns the token to give to _release_pushes."""
        self._held = held = {}
        return held

    def _release_pushes(self, token: dict, before: dict) -> None:
        """Send every held group whose latest value differs from `before`, in the order they were first pushed.
        A newer hold keeps its own collection."""
        if self._held is token:
            self._held = None
        for name in token:
            data = self.groups.get(name)
            if data != before.get(name) and self.server is not None:
                self.server.push_state(name, data)

    def _push_event(self, name: str, data: dict | None = None) -> None:
        if self.server is not None:
            self.server.push_event(name, data)

    def _snapshot(self) -> dict:
        return dict(self.groups)

    # --- sleep and shutdown announcements (logind, see lifecycle.py) -------------

    async def _start_lifecycle(self) -> None:
        self.lifecycle = Lifecycle(on_sleep=self._announce_sleep, on_shutdown=self._announce_shutdown,
                                   on_resume=self._announce_resume)
        await self.lifecycle.start()

    async def _stop_lifecycle(self) -> None:
        if self.lifecycle is not None:
            await self.lifecycle.stop()

    def _lifecycle_active(self) -> bool:
        return self.lifecycle is not None and self.lifecycle.active

    def _sleep_protected(self) -> bool:
        """True when logind announces the sleep for us (every path: button, idle timer, Home Assistant, Steam menu)."""
        return self.lifecycle is not None and self.lifecycle.protecting

    async def _announce_sleep(self) -> None:
        self._push_event("going_to_sleep", {"reason": "suspend"})
        await asyncio.gather(self._flush_server(), self._sendspin_going_down("shutdown"))

    async def _announce_shutdown(self, reason: str) -> None:
        self._push_event("shutting_down", {"reason": reason})
        await asyncio.gather(self._flush_server(), self._sendspin_going_down(reason))

    async def _flush_server(self) -> None:
        if self.server is not None:
            await self.server.flush()

    async def _sendspin_going_down(self, reason: str) -> None:
        """Let the Sendspin player leave cleanly while the announcement is flushed (never delays or breaks the sleep)."""
        hook = getattr(self, "_sendspin_sleep", None)
        if hook is not None:
            await hook(reason)

    async def _device_group(self) -> dict:
        """Identity, network and Wake on LAN facts the integration needs while the device is awake."""
        s = self._load_settings()
        wol = self._wol_cache
        return {
            "device_id": s["device_id"], "name": s["device_name"], "protocol": PROTOCOL_VERSION, "session": self.session,
            "mac": wol.get("mac", ""), "broadcast": wol.get("broadcast", "255.255.255.255"),
            "wol": {k: wol[k] for k in ("interface", "kind", "supported", "wol_enabled", "warnings", "error") if k in wol},
        }

    async def _refresh_wol(self) -> None:
        try:
            self._wol_cache = await asyncio.get_running_loop().run_in_executor(None, wol_info)
        except Exception as e:  # noqa: BLE001
            decky.logger.warning(f"Network info failed: {e}")
            self._wol_cache = {"interface": "", "error": str(e)}

    async def _push_device(self) -> None:
        await self._refresh_wol()
        await self._push_device_group()

    async def _push_device_group(self) -> None:
        group = await self._device_group()
        if group != self.groups.get("device"):
            self._push("device", group)

    async def _device_loop(self) -> None:
        while True:
            await self._push_device()
            await asyncio.sleep(DEVICE_REFRESH_SECONDS)

    async def _check_session(self) -> None:
        """Notice a switch between game and desktop mode; the Steam UI (and so the running-app report) is gone
        in desktop mode."""
        session = await asyncio.get_running_loop().run_in_executor(None, detect_session)
        if session == "unknown" or session == self.session:
            return
        decky.logger.info(f"Session: {session}")
        self.session = session
        if session == "desktop":
            self._set_app_state(appid="", name="")
        elif self.app_reported:
            self._set_app_state()
        await self._push_device_group()

    async def _session_loop(self) -> None:
        while True:
            await self._check_session()
            await asyncio.sleep(SESSION_CHECK_SECONDS)

    async def _resume_loop(self) -> None:
        wall, mono = time.time(), time.monotonic()
        while True:
            await asyncio.sleep(RESUME_CHECK_SECONDS)
            now_wall, now_mono = time.time(), time.monotonic()
            if self._lifecycle_active():
                wall, mono = now_wall, now_mono  # logind announces the resume; this is only the fallback
                continue
            if (now_wall - wall) - (now_mono - mono) > RESUME_JUMP_SECONDS:
                await self._announce_resume()
            wall, mono = now_wall, now_mono
