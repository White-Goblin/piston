"""Camera picture-in-picture: validate commands from the integration and forward them to the frontend.

The integration supplies every option per call (its options hold the defaults); the device keeps none.
The signalling relay (WebRTC/HLS through the integration) lives in pip_signal.py.
"""
import re

import decky

from .proto import messages as m

PIP_POSITIONS = (
    "top", "top-right", "right", "bottom-right",
    "bottom", "bottom-left", "left", "top-left",
)
PIP_ENTITY_RE = re.compile(r"^camera\.[a-z0-9_]+$")
# Size is the width of a 16:9 bounding box as a fraction of the screen width
PIP_SIZES = {"small": 0.25, "medium": 0.4, "large": 0.55, "extra_large": 0.7}
PIP_ACTIVE_STATES = ("loading", "connecting", "negotiating", "playing")
DEFAULT_POSITION, DEFAULT_SIZE, DEFAULT_DURATION_MS = "top-right", PIP_SIZES["medium"], 30_000


def _bad(message: str) -> m.ProtocolError:
    return m.ProtocolError(m.BAD_REQUEST, message)


class PipMixin:
    async def _cmd_pip_show(self, data: dict) -> dict:
        self._require_game_mode()
        entity_id = str(data.get("camera", ""))
        if not PIP_ENTITY_RE.match(entity_id):
            raise _bad("camera must be a camera entity, e.g. camera.doorbell")
        position = str(data.get("position", DEFAULT_POSITION))
        if position not in PIP_POSITIONS:
            raise _bad(f"position must be one of {', '.join(PIP_POSITIONS)}")
        size = data.get("size", DEFAULT_SIZE)
        if isinstance(size, bool) or not isinstance(size, (int, float)) or not 0.1 <= size <= 0.9:
            raise _bad("size must be between 0.1 and 0.9 (fraction of screen width)")
        duration = data.get("duration", DEFAULT_DURATION_MS)
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            raise _bad("duration must be >= 0 (ms, 0 = until hidden)")
        session = str(data.get("session", ""))
        self._pip_open(session)  # also closes the previous PiP's session
        await decky.emit("pip", {"action": "show", "entity_id": entity_id, "session": session, "position": position,
                                 "size": float(size), "duration": duration, "mute": bool(data.get("mute", True))})
        return {}

    async def _cmd_pip_hide(self, data: dict) -> dict:
        self._pip_close_all(notify=False)
        await decky.emit("pip", {"action": "hide"})
        return {}

    def set_pip_state(self, state: str, entity_id: str = "", message: str = "") -> None:
        self.pip_state = {"state": state, "entity_id": entity_id, "message": message}
        self._push("pip", self.pip_state)

    async def report_pip_state(self, state: str, entity_id: str = "", message: str = "") -> None:
        self.set_pip_state(state, entity_id, message)

    async def show_pip(self, entity_id: str) -> None:
        """Panel/test hook: show a camera with defaults (the integration normally drives this)."""
        import secrets
        await self._cmd_pip_show({"camera": entity_id, "session": secrets.token_urlsafe(12)})

    async def hide_pip(self) -> None:
        await self._cmd_pip_hide({})

    async def get_pip_config(self) -> dict:
        return {"port": self.http_port, "nonce": self.nonce}
