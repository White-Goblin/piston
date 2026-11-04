import asyncio
import os
import secrets
import sys

# Decky has no runtime pip, so any dependency would be vendored in py_modules/ (there are none now)
_here = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_here, "py_modules"))
sys.path.insert(0, _here)

import decky

from piston_device.advertise import Advertiser
from piston_device.audio import AudioMixin
from piston_device.control import ControlMixin
from piston_device.controllers import ControllersMixin
from piston_device.http_server import HttpServerMixin
from piston_device.media import MediaMixin
from piston_device.media_keys import MediaKeyMixin
from piston_device.pairing import PairingMixin
from piston_device.pip import PipMixin
from piston_device.pip_signal import PipSignalMixin
from piston_device.resume import ResumeMixin
from piston_device.sensors import SensorsMixin
from piston_device.proto import LANE_VERSION, messages as proto_messages
from piston_device.server import DEFAULT_PORT, ProtocolServer
from piston_device.settings import SettingsMixin
from piston_device.state import StateMixin
from piston_device.update import UpdateMixin
from piston_device.version import plugin_version
from piston_device.wol import WolMixin


class Plugin(
    SettingsMixin,
    StateMixin,
    ResumeMixin,
    PairingMixin,
    ControlMixin,
    AudioMixin,
    MediaMixin,
    MediaKeyMixin,
    ControllersMixin,
    SensorsMixin,
    PipMixin,
    PipSignalMixin,
    WolMixin,
    UpdateMixin,
    HttpServerMixin,
):
    def _commands(self) -> dict:
        """Protocol command name -> async handler taking the command's data object."""
        return {
            "launch": self._cmd_launch,
            "quit": self._cmd_quit,
            "power": self._cmd_power,
            "audio": self._cmd_audio,
            "media_key": self._cmd_media_key,
            "get_artwork": self._cmd_get_artwork,
            "toast": self._cmd_toast,
            "pip_show": self._cmd_pip_show,
            "pip_hide": self._cmd_pip_hide,
            "pip_signal": self._cmd_pip_signal,
            "set_sensor_interval": self._cmd_set_sensor_interval,
            "unpair": self._cmd_unpair,
        }

    def _pairing_changed(self) -> None:
        super()._pairing_changed()
        advertiser = getattr(self, "advertiser", None)
        if advertiser is not None and self.loop.is_running():
            self.loop.create_task(advertiser.sync())

    def _on_link(self, connected: bool) -> None:
        if not connected and self.pip_sessions:
            self._pip_close_all(notify=False)  # nobody left to relay for
            self.loop.create_task(decky.emit("pip", {"action": "hide"}))
        self.loop.create_task(decky.emit("link", {"connected": connected}))
        if connected:
            self._resend_update_status()

    def _advert_info(self) -> dict:
        s = self._load_settings()
        return {"device_id": s["device_id"], "name": s["device_name"], "paired": bool(s["secret"])}

    def _hello_extra(self) -> dict:
        extra = {"name": self._load_settings()["device_name"]}
        version = plugin_version()
        if version is not None:
            extra.update(proto_messages.device_hello_extra(version, LANE_VERSION))
        return extra

    # --- panel callables --------------------------------------------------

    async def get_status(self) -> dict:
        s = self._load_settings()
        return {
            "device_id": s["device_id"],
            "paired": bool(s["secret"]),
            "connected": self.server is not None and self.server.connected,
            "port": self.server.port if self.server is not None else 0,
            "error": self.error,
        }

    # --- lifecycle --------------------------------------------------------

    async def _main(self):
        self.loop = asyncio.get_event_loop()
        self.error = ""
        self.nonce = secrets.token_urlsafe(16)
        self.pip_state = {"state": "hidden", "entity_id": "", "message": ""}
        self.app_focus = ""
        self.app_reported = False  # no apps group is pushed before the frontend reports
        self.controller_state = {}
        self.os_controllers = {}
        self.readings = None  # last real battery reading per controller, loaded lazily
        self.app_state = {"running": False, "appid": "", "name": "", "screen": "steam", "playback": "unknown"}
        self.sensor_wake = asyncio.Event()
        self.sensor_values = {}
        self.cpu_prev = None
        self.http_server = None
        self.http_port = 0
        self.advertiser = None
        self._init_state()
        self._init_media()
        self._init_pairing()
        self._init_pip_signal()
        self._init_update()
        decky.logger.info("Piston starting")

        self.server = ProtocolServer(
            device_id=lambda: self._load_settings()["device_id"],
            secret=lambda: bytes.fromhex(self._load_settings()["secret"] or ""),
            pairing=self.pairing,
            commands=self._commands(),
            snapshot=self._snapshot,
            port=DEFAULT_PORT,
            on_connection=self._on_link,
            hello_extra=self._hello_extra,
            on_update=self._on_update_frame,
            update_status=self._update_status,
        )
        try:
            await self.server.start()
        except OSError as e:
            self.error = f"Could not listen on port {DEFAULT_PORT}: {e}"
            decky.logger.error(self.error)
            self.server.port = 0
        await self._start_http_server()
        await self._detect_audio_mode()
        await self._push_audio()
        self._push("pip", self.pip_state)
        self._publish_controllers()
        self.sensor_task = asyncio.create_task(self._sensor_loop())
        self.audio_task = asyncio.create_task(self._audio_mode_loop())
        self.device_task = asyncio.create_task(self._device_loop())
        self._update_startup()
        self.session_task = asyncio.create_task(self._session_loop())
        self.resume_task = asyncio.create_task(self._resume_loop())
        self.playback_task = asyncio.create_task(self._playback_loop())
        if self.server.port:
            self.advertiser = Advertiser(self._advert_info, self.server.port)
            await self.advertiser.start()
        await self._start_lifecycle()

    async def _unload(self):
        decky.logger.info("Piston unloading")
        # Decky may unload a plugin that is still starting (an install right after a start): only stop what exists
        for name in ("sensor_task", "audio_task", "device_task", "session_task", "resume_task", "playback_task",
                     "update_task", "update_confirm_task", "update_approval_task"):
            task = getattr(self, name, None)
            if task is not None:
                task.cancel()
        if getattr(self, "lifecycle", None) is not None:
            await self._stop_lifecycle()
        advertiser = getattr(self, "advertiser", None)
        if advertiser is not None:
            await advertiser.stop()
        server = getattr(self, "server", None)
        if server is not None:
            await server.stop()
        if getattr(self, "keyboard", None) is not None:
            self._close_keyboard()
        http_server = getattr(self, "http_server", None)
        if http_server is not None:
            http_server.close()

    async def _uninstall(self):
        decky.logger.info("Piston uninstalled")
