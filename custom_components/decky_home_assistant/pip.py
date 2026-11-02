"""Picture-in-picture: relays the player's WebRTC/HLS signalling between the device and a camera entity.

Device player -> integration: event `pip_signal` {session, kind: start | offer | candidate | hls | close}.
Integration -> device player: command `pip_signal` {session, kind: config | answer | candidate | hls_url | error}.
The media itself flows directly between the device and Home Assistant's camera stack (go2rtc / the HLS views).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Callable

from .client import CommandError, DeviceOffline

_LOGGER = logging.getLogger(__name__)

CANDIDATE_RETRIES = 10
CANDIDATE_RETRY_SECONDS = 0.2


def default_get_camera(hass, entity_id: str):
    from homeassistant.components.camera.helper import get_camera_from_entity_id

    return get_camera_from_entity_id(hass, entity_id)


def default_candidate(data: dict):
    from webrtc_models import RTCIceCandidateInit

    return RTCIceCandidateInit.from_dict(data)


def default_base_url(hass) -> str:
    """Where the device can reach Home Assistant's HLS views: the internal URL, else the local address."""
    from homeassistant.helpers.network import NoURLAvailableError, get_url

    try:
        return get_url(hass, allow_external=False, allow_cloud=False, prefer_external=False).rstrip("/")
    except NoURLAvailableError:
        return f"http://{hass.config.api.local_ip}:{hass.config.api.port}"


class PipRelay:
    def __init__(self, hass, coordinator, *, get_camera: Callable = default_get_camera,
                 parse_candidate: Callable = default_candidate, base_url: Callable = default_base_url):
        self.hass, self.coordinator = hass, coordinator
        self._get_camera, self._parse_candidate, self._base_url = get_camera, parse_candidate, base_url
        self.sessions: dict[str, str] = {}  # session -> camera entity id
        self._tasks: set[asyncio.Task] = set()
        self._remove: list[Callable] = []

    # --- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self._remove.append(self.coordinator.async_add_event_listener("pip_signal", self._on_signal))
        self._remove.append(self.coordinator.async_add_listener(self._on_state))

    def stop(self) -> None:
        for remove in self._remove:
            remove()
        self._remove.clear()
        self._close_all()
        for task in list(self._tasks):
            task.cancel()

    def _on_state(self) -> None:
        if not self.coordinator.connected and self.sessions:
            self._close_all()

    def _close_all(self) -> None:
        for session in list(self.sessions):
            self._close(session)

    def _close(self, session: str) -> None:
        entity_id = self.sessions.pop(session, None)
        if entity_id is None:
            return
        try:
            self._get_camera(self.hass, entity_id).close_webrtc_session(session)
        except Exception as e:  # noqa: BLE001 - the camera may be gone already
            _LOGGER.debug("closing PiP session %s: %s", session, e)

    async def async_wait_idle(self) -> None:
        """Wait for in-flight signalling work (used by tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- starting and stopping the overlay ----------------------------------------

    async def async_show(self, camera: str, options: dict) -> str:
        """Ask the device to show `camera`; returns the signalling session id."""
        session = secrets.token_urlsafe(12)
        await self.coordinator.async_command("pip_show", {"camera": camera, "session": session, **options})
        self._close_all()  # the device replaced any earlier PiP
        self.sessions[session] = camera
        return session

    async def async_hide(self) -> None:
        await self.coordinator.async_command("pip_hide")
        self._close_all()

    # --- signalling from the player ------------------------------------------------

    def _on_signal(self, data: dict) -> None:
        task = asyncio.get_running_loop().create_task(self._handle(data))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, session: str, message: dict) -> None:
        try:
            await self.coordinator.async_command("pip_signal", {"session": session, **message})
        except (DeviceOffline, CommandError) as e:
            _LOGGER.debug("PiP signal to the device failed (%s); dropping session %s", e, session)
            self.sessions.pop(session, None)

    async def _handle(self, data: dict) -> None:
        session, kind = data.get("session"), data.get("kind")
        camera_id = self.sessions.get(session)
        if kind == "close":
            self._close(session)
            return
        if camera_id is None:
            return  # a late message for a session that already ended
        try:
            camera = self._get_camera(self.hass, camera_id)
            if kind == "start":
                await self._start(session, camera)
            elif kind == "offer":
                await camera.async_handle_async_webrtc_offer(data["sdp"], session, self._webrtc_sender(session))
            elif kind == "candidate":
                await self._candidate(session, camera, data.get("candidate") or {})
            elif kind == "hls":
                await self._hls(session, camera)
        except Exception as e:  # noqa: BLE001 - every failure goes back to the player, which falls back or reports
            _LOGGER.warning("PiP %s for %s failed: %s", kind, camera_id, e)
            await self._send(session, {"kind": "error", "code": f"pip_{kind}_failed", "message": str(e)})

    async def _start(self, session: str, camera) -> None:
        webrtc = "web_rtc" in camera.camera_capabilities.frontend_stream_types
        message = {"kind": "config", "webrtc": webrtc}
        if webrtc:
            message["config"] = camera.async_get_webrtc_client_configuration().to_frontend_dict()
        await self._send(session, message)

    def _webrtc_sender(self, session: str) -> Callable[[Any], None]:
        def send(message) -> None:
            body = message.as_dict()
            if body.get("type") == "session":
                return  # the device already knows the session
            task = asyncio.get_running_loop().create_task(self._send(session, {"kind": body["type"], **body}))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return send

    async def _candidate(self, session: str, camera, candidate: dict) -> None:
        parsed = self._parse_candidate(candidate)
        for attempt in range(CANDIDATE_RETRIES):
            try:
                await camera.async_on_webrtc_candidate(session, parsed)
                return
            except Exception:  # noqa: BLE001 - the provider may not know the session yet (offer still starting)
                if attempt == CANDIDATE_RETRIES - 1:
                    raise
                await asyncio.sleep(CANDIDATE_RETRY_SECONDS)

    async def _hls(self, session: str, camera) -> None:
        stream = await camera.async_create_stream()
        if stream is None:
            raise RuntimeError("this camera has no stream")
        stream.add_provider("hls")
        await stream.start()
        await self._send(session, {"kind": "hls_url", "url": self._base_url(self.hass) + stream.endpoint_url("hls")})
