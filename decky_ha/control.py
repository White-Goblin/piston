"""Device control commands: launch/quit apps, power, notifications, sensor interval."""
import asyncio
import re
import subprocess
import time

import decky

from .proto import messages as m
from .settings import APPID_RE, SENSOR_INTERVALS

POWER_ACTIONS = ("suspend", "restart", "shutdown", "restart_steam", "desktop", "game")
ICON_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
APP_WAIT_SECONDS = 15
SLEEP_FLUSH_SECONDS = 0.4


def _bad(message: str) -> m.ProtocolError:
    return m.ProtocolError(m.BAD_REQUEST, message)


def _game_mode_only() -> m.ProtocolError:
    return m.ProtocolError(m.UNSUPPORTED, "this only works in game mode (the Steam interface is not running on the desktop)")


class ControlMixin:
    def _require_game_mode(self) -> None:
        if getattr(self, "session", "game") == "desktop":
            raise _game_mode_only()

    # --- session switching ---------------------------------------------------

    def _switch_session(self, desktop: bool) -> None:
        # steamos-session-select has no plain "desktop" session; steamosctl switches to the device's
        # default desktop session. Older images only have the script.
        attempts = (
            [["steamosctl", "switch-to-desktop-mode"], ["steamos-session-select", "plasma-wayland"]]
            if desktop else
            [["steamosctl", "switch-to-game-mode"], ["steamos-session-select", "gamescope"]]
        )
        last = ""
        for command in attempts:
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            except (OSError, subprocess.SubprocessError) as e:
                last = f"{command[0]}: {e}"
                continue
            if result.returncode == 0:
                return
            last = f"{command[0]}: {result.stderr.strip() or result.returncode}"
        decky.logger.warning(f"Session switch failed: {last}")
        raise m.ProtocolError(m.INTERNAL, f"could not switch session ({last})")

    async def log_warning(self, message: str) -> None:
        """Let the frontend put a warning in the plugin log."""
        decky.logger.warning(str(message)[:300])

    # --- running app and screen ----------------------------------------------

    def _app_screen(self) -> str:
        """What is in front: the running app, Steam's interface over it, or Steam alone."""
        if getattr(self, "session", "game") == "desktop":
            return "desktop"
        if not self.app_state["running"]:
            return "steam"
        return "steam_over_game" if self.app_focus == "steam" else "game"

    def _set_app_state(self, **changes) -> None:
        state = {**self.app_state, **changes}
        state["running"] = bool(state["appid"])
        state["screen"] = "steam"
        self.app_state = state
        self.app_state["screen"] = self._app_screen()
        self.app_reported = True
        self._push_apps()

    def _push_apps(self) -> None:
        # Until the frontend has reported, the defaults would briefly say "not running"
        if self.app_reported:
            self._push("apps", {"apps": self._load_settings()["apps"], **self.app_state})

    async def report_app_state(self, appid: str = "", name: str = "") -> None:
        """Frontend reports the running Steam app (empty when none)."""
        self._set_app_state(appid=appid, name=name)

    async def report_app_focus(self, focus: str = "") -> None:
        """Frontend reports which window has focus: "steam" (Steam's interface) or "app"."""
        if focus != self.app_focus:
            self.app_focus = focus
            self._set_app_state()

    async def _wait_for_app(self, appid: str, timeout: float | None = None) -> bool:
        """Wait until the running app is `appid` ("" = nothing running)."""
        deadline = asyncio.get_running_loop().time() + (APP_WAIT_SECONDS if timeout is None else timeout)
        while self.app_state["appid"] != appid:
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.1)
        return True

    # --- commands ------------------------------------------------------------

    async def _cmd_launch(self, data: dict) -> dict:
        self._require_game_mode()
        appid = str(data.get("appid", ""))
        if not APPID_RE.match(appid):
            raise _bad("appid must be a Steam app id (digits)")
        if not any(a["appid"] == appid for a in self._load_settings()["apps"]):
            raise _bad("that app is not offered in the plugin panel")
        running = self.app_state["appid"]
        if running == appid:
            return {"started": True, "already_running": True}
        if running:
            await decky.emit("app", {"action": "quit", "appid": running})
            if not await self._wait_for_app(""):
                raise m.ProtocolError(m.INTERNAL, "the running app did not close")
        await decky.emit("app", {"action": "launch", "appid": appid})
        return {"started": await self._wait_for_app(appid)}

    async def _cmd_quit(self, data: dict) -> dict:
        self._require_game_mode()
        running = self.app_state["appid"]
        if not running:
            return {}
        await decky.emit("app", {"action": "quit", "appid": running})
        if not await self._wait_for_app(""):
            raise m.ProtocolError(m.INTERNAL, "the running app did not close")
        return {}

    async def _cmd_power(self, data: dict) -> dict:
        action = data.get("action")
        if action not in POWER_ACTIONS:
            raise _bad(f"action must be one of {', '.join(POWER_ACTIONS)}")
        decky.logger.info(f"Power action: {action}")
        if action in ("desktop", "game"):
            await asyncio.get_running_loop().run_in_executor(None, self._switch_session, action == "desktop")
            return {}
        self._require_game_mode()
        if action == "suspend":
            # Tell the integration before the connection goes away so it shows standby, not a drop
            self._push_event("going_to_sleep")
            await asyncio.sleep(SLEEP_FLUSH_SECONDS)
        await decky.emit("power", {"action": action})
        return {}

    async def _cmd_toast(self, data: dict) -> dict:
        self._require_game_mode()
        message = data.get("message")
        if not isinstance(message, str) or not message.strip():
            raise _bad("message is required")
        title = str(data.get("title") or "Home Assistant")
        if len(message) > 500 or len(title) > 100:
            raise _bad("title or message too long")
        try:
            duration = int(data.get("duration", 5000))
        except (TypeError, ValueError):
            raise _bad("duration must be a number of milliseconds") from None
        if not 0 <= duration <= 600_000:
            raise _bad("duration must be between 0 and 600000 ms")
        toast = {"title": title, "message": message, "duration": duration, "silent": bool(data.get("silent", False))}
        icon = data.get("icon")
        if icon is not None:
            if not isinstance(icon, str) or not ICON_RE.match(icon):
                raise _bad("icon must be a name like 'info'")
            toast["icon"] = icon
        if data.get("id") is not None:
            toast["id"] = str(data["id"])[:80]
        await decky.emit("notification", toast)
        return {}

    async def _cmd_set_sensor_interval(self, data: dict) -> dict:
        seconds = data.get("seconds")
        if seconds not in SENSOR_INTERVALS:
            raise _bad(f"seconds must be one of {', '.join(map(str, SENSOR_INTERVALS))}")
        self._update_settings({"sensor_interval": seconds})
        self.sensor_wake.set()
        return {}

    async def report_toast_click(self, id: str = "") -> None:
        """Frontend: the user clicked a toast."""
        self._push_event("toast_click", {"id": str(id)[:80], "at": time.time()})
