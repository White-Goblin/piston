"""Volume and mute: HDMI CEC for a CEC soundbar/TV, otherwise PipeWire (wpctl)."""
import asyncio
import os
import re
import subprocess

import decky

from .proto import messages as m

# Steam's session user runs the audio server; the plugin backend needs its runtime dir
RUNTIME_DIR = f"/run/user/{os.getuid()}" if os.getuid() else "/run/user/1000"

# SteamOS's CEC daemon (cecd) on the session bus. With an HDMI soundbar the real
# volume is CEC: PipeWire's own gain is fixed, so wpctl has no audible effect.
CEC_BUS = "com.steampowered.CecDaemon1"
CEC_PATH = "/com/steampowered/CecDaemon1/Devices/Cec0"
CEC_IFACE = "com.steampowered.CecDaemon1.CecDevice1"
CEC_METHODS = {"volume_up": "VolumeUp", "volume_down": "VolumeDown", "mute": "Mute"}
CEC_LINK_DOWN = "HDMI CEC has no link right now. Check that the soundbar or TV is on, then try again."
CEC_MAX_STEPS = 20
CEC_STEP_DELAY = 0.15
AUDIO_MODE_CHECK_SECONDS = 30
_HDMI_SINK_RE = re.compile(r'(?:api\.alsa\.path|node\.name) = "[^"]*hdmi', re.IGNORECASE)


class AudioMixin:
    audio_mode = "system"  # "cec" or "system"
    cec_link_ok = True     # only for logging the link dropping and coming back

    def _busctl(self, *args: str) -> str:
        env = {
            **os.environ,
            "XDG_RUNTIME_DIR": RUNTIME_DIR,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={RUNTIME_DIR}/bus",
        }
        result = subprocess.run(
            ["busctl", "--user", *args], capture_output=True, text=True, timeout=5, env=env
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"busctl exited with {result.returncode}")
        return result.stdout.strip()

    def _default_sink_is_hdmi(self) -> bool:
        """CEC only applies while audio plays out over HDMI (not headphones or a USB DAC)."""
        try:
            return bool(_HDMI_SINK_RE.search(self._wpctl("inspect", "@DEFAULT_AUDIO_SINK@")))
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return False

    def _cec_audio_address(self):
        """Logical address of the CEC audio device (soundbar/TV), or None without active CEC."""
        if not self._default_sink_is_hdmi():
            return None
        try:
            active = self._busctl("get-property", CEC_BUS, CEC_PATH, CEC_IFACE, "Active")
            address = self._busctl("get-property", CEC_BUS, CEC_PATH, CEC_IFACE, "AudioLogicalAddress")
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return None
        if active.split()[-1] != "true":
            return None
        parts = address.split()
        return int(parts[-1]) if parts[-1].isdigit() else None

    async def _detect_audio_mode(self) -> bool:
        """Re-detect CEC vs system volume; returns True when the mode changed."""
        address = await self.loop.run_in_executor(None, self._cec_audio_address)
        mode = "cec" if address is not None else "system"
        changed = mode != self.audio_mode
        if changed:
            decky.logger.info(f"Audio control mode: {mode}")
        self.audio_mode = mode
        return changed

    async def _audio_mode_loop(self):
        """Follow output changes (CEC link coming up, headphones plugged in) without a restart."""
        while True:
            await asyncio.sleep(AUDIO_MODE_CHECK_SECONDS)
            if await self._detect_audio_mode():
                await self._push_audio()

    def _cec_link_up(self) -> bool:
        """Whether the CEC daemon holds a logical address for this device. It gives the address up when the HDMI device on the other
        end (soundbar, TV) stops answering polls, and every call then fails ("The device does not have a logical address") while the
        daemon still reports CEC as active. When this cannot be read, the call is left to decide."""
        try:
            parts = self._busctl("get-property", CEC_BUS, CEC_PATH, CEC_IFACE, "LogicalAddresses").split()   # "ay 1 4", "ay 0" when none
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return True
        return int(parts[1]) > 0 if len(parts) >= 2 and parts[0] == "ay" and parts[1].isdigit() else True

    def _cec_link_lost(self) -> m.ProtocolError:
        if self.cec_link_ok:                          # log the change, not every press
            decky.logger.warning("HDMI CEC has no logical address: the soundbar or TV is not answering")
            self.cec_link_ok = False
        return m.ProtocolError(m.BUSY, CEC_LINK_DOWN)

    def _cec_press(self, action: str) -> None:
        address = self._cec_audio_address()
        if address is None:
            raise RuntimeError("HDMI CEC is not active")
        if not self._cec_link_up():
            raise self._cec_link_lost()
        try:
            self._busctl("call", CEC_BUS, CEC_PATH, CEC_IFACE, CEC_METHODS[action], "y", str(address))
        except RuntimeError as e:
            if "logical address" in str(e).lower():  # the link dropped between the check and the call
                raise self._cec_link_lost() from None
            raise
        if not self.cec_link_ok:
            decky.logger.info("HDMI CEC link is back")
            self.cec_link_ok = True

    def _wpctl(self, *args: str) -> str:
        env = {**os.environ, "XDG_RUNTIME_DIR": RUNTIME_DIR}
        result = subprocess.run(
            ["wpctl", *args], capture_output=True, text=True, timeout=5, env=env
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"wpctl exited with {result.returncode}")
        return result.stdout.strip()

    def _audio_state(self):
        match = re.match(r"Volume:\s*([\d.]+)(\s*\[MUTED\])?", self._wpctl("get-volume", "@DEFAULT_AUDIO_SINK@"))
        if not match:
            return None
        return {"volume": round(float(match.group(1)) * 100), "muted": bool(match.group(2))}

    async def _push_audio(self) -> None:
        """Audio state group: the mode, plus level and mute only where the system mixer can read them."""
        group = {"mode": self.audio_mode}
        if self.audio_mode == "system":
            try:
                state = await self.loop.run_in_executor(None, self._audio_state)
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                decky.logger.warning(f"Audio state read failed: {e}")
                state = None
            if state:
                group.update(state)
        self._push("audio", group)

    async def _cmd_audio(self, data: dict) -> dict:
        action = data.get("action")
        run = self.loop.run_in_executor
        try:
            if self.audio_mode == "cec":
                if action not in CEC_METHODS:
                    raise m.ProtocolError(m.BAD_REQUEST, f"action must be one of {', '.join(CEC_METHODS)}")
                steps = data.get("steps", 1)
                if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= CEC_MAX_STEPS:
                    raise m.ProtocolError(m.BAD_REQUEST, f"steps must be between 1 and {CEC_MAX_STEPS}")
                for i in range(1 if action == "mute" else steps):
                    if i:
                        await asyncio.sleep(CEC_STEP_DELAY)
                    await run(None, self._cec_press, action)
                return {}
            if action == "set_volume":
                level = data.get("value")
                if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0 <= level <= 100:
                    raise m.ProtocolError(m.BAD_REQUEST, "value must be between 0 and 100")
                await run(None, self._wpctl, "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level / 100:.2f}")
            elif action == "set_mute":
                if not isinstance(data.get("value"), bool):
                    raise m.ProtocolError(m.BAD_REQUEST, "value must be true or false")
                await run(None, self._wpctl, "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if data["value"] else "0")
            else:
                raise m.ProtocolError(m.UNSUPPORTED, "this output is controlled with system volume (set_volume, set_mute)"
                                      if action in CEC_METHODS else "unknown audio action")
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            raise m.ProtocolError(m.INTERNAL, f"audio command failed: {e}") from None
        await self._push_audio()
        return {}
