"""The device as a media player: power, apps as sources, transport keys and volume."""
from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass, MediaPlayerEntity, MediaPlayerEntityFeature, MediaPlayerState,
)

from homeassistant.exceptions import HomeAssistantError

from .const import CONF_MAC, OPT_TURN_OFF
from .entity import DeckyEntity
from .wol import send_magic_packet

STEAM_SOURCE = "Steam"
FEATURES = (
    MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF | MediaPlayerEntityFeature.SELECT_SOURCE | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE | MediaPlayerEntityFeature.PREVIOUS_TRACK | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.VOLUME_MUTE | MediaPlayerEntityFeature.VOLUME_STEP
)
SYSTEM_VOLUME_STEP = 5


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DeckyMediaPlayer(entry.runtime_data)])


class DeckyMediaPlayer(DeckyEntity, MediaPlayerEntity):
    _attr_name = None  # the device's own name
    _attr_device_class = MediaPlayerDeviceClass.TV

    def __init__(self, coordinator):
        super().__init__(coordinator, "media_player")
        self._assumed_muted = False  # over CEC, mute is a toggle with no readable state

    @property
    def available(self) -> bool:
        return True  # off and standby are states, not unavailability

    # --- state ----------------------------------------------------------------

    @property
    def _apps_group(self) -> dict:
        return self.coordinator.groups.get("apps", {})

    @property
    def _audio(self) -> dict:
        return self.coordinator.groups.get("audio", {})

    @property
    def state(self):
        power = self.coordinator.power
        if power == "off":
            return MediaPlayerState.OFF
        if power == "standby":
            return MediaPlayerState.STANDBY
        return MediaPlayerState.PLAYING if self._apps_group.get("running") else MediaPlayerState.IDLE

    @property
    def supported_features(self):
        features = FEATURES
        if self._audio.get("mode") == "system":
            features |= MediaPlayerEntityFeature.VOLUME_SET
        return features

    @property
    def _sources(self) -> dict:
        """Source name -> appid. Duplicate names get their appid appended so every source is selectable."""
        apps = self._apps_group.get("apps", [])
        names = [a["name"] for a in apps]
        return {(a["name"] if names.count(a["name"]) == 1 and a["name"] != STEAM_SOURCE else f"{a['name']} ({a['appid']})"):
                a["appid"] for a in apps}

    @property
    def source_list(self) -> list:
        return [STEAM_SOURCE, *self._sources]

    @property
    def source(self):
        group = self._apps_group
        if group.get("screen") == "desktop":
            return None
        if not group.get("running"):
            return STEAM_SOURCE
        for name, appid in self._sources.items():
            if appid == group.get("appid"):
                return name
        return None  # something launched outside the offered list

    @property
    def media_title(self):
        return self._apps_group.get("name") or None

    @property
    def app_name(self):
        return self._apps_group.get("name") or None

    @property
    def volume_level(self):
        if self._audio.get("mode") == "system" and "volume" in self._audio:
            return self._audio["volume"] / 100
        return None

    @property
    def is_volume_muted(self):
        if self._audio.get("mode") == "system":
            return self._audio.get("muted")
        return self._assumed_muted

    @property
    def extra_state_attributes(self) -> dict:
        return {"steam_screen": self._apps_group.get("screen"), "audio_mode": self._audio.get("mode")}

    # --- commands -------------------------------------------------------------

    async def async_turn_on(self) -> None:
        """Wake the device with a magic packet (works from standby and, if the hardware allows, from off)."""
        if self.coordinator.power == "on" and self.coordinator.online:
            return  # already awake
        device = self.coordinator.groups.get("device", {})
        mac = device.get("mac") or self.coordinator.entry.data.get(CONF_MAC)
        if not mac:
            raise HomeAssistantError("The device's MAC address is not known yet. Turn it on once by hand while connected.")
        try:
            await send_magic_packet(mac, device.get("broadcast") or "255.255.255.255")
        except (OSError, ValueError) as e:
            raise HomeAssistantError(f"Could not send the wake packet: {e}") from None

    async def async_turn_off(self) -> None:
        action = "shutdown" if self.coordinator.options[OPT_TURN_OFF] == "shutdown" else "suspend"
        await self._command("power", {"action": action})

    async def async_select_source(self, source: str) -> None:
        if source == STEAM_SOURCE:
            await self._command("quit")
            return
        appid = self._sources.get(source)
        if appid is None:
            raise HomeAssistantError(f"Unknown source {source}")
        await self._command("launch", {"appid": appid})

    async def async_media_play(self) -> None:
        await self._command("media_key", {"key": "play"})

    async def async_media_pause(self) -> None:
        await self._command("media_key", {"key": "pause"})

    async def async_media_play_pause(self) -> None:
        await self._command("media_key", {"key": "play_pause"})

    async def async_media_next_track(self) -> None:
        await self._command("media_key", {"key": "next"})

    async def async_media_previous_track(self) -> None:
        await self._command("media_key", {"key": "previous"})

    async def async_volume_up(self) -> None:
        await self._step(+1)

    async def async_volume_down(self) -> None:
        await self._step(-1)

    async def _step(self, direction: int) -> None:
        if self._audio.get("mode") == "system":
            level = round((self.volume_level or 0) * 100) + direction * SYSTEM_VOLUME_STEP
            await self._command("audio", {"action": "set_volume", "value": max(0, min(100, level))})
        else:
            await self._command("audio", {"action": "volume_up" if direction > 0 else "volume_down"})

    async def async_set_volume_level(self, volume: float) -> None:
        await self._command("audio", {"action": "set_volume", "value": round(volume * 100)})

    async def async_mute_volume(self, mute: bool) -> None:
        if self._audio.get("mode") == "system":
            await self._command("audio", {"action": "set_mute", "value": mute})
            return
        if mute != self._assumed_muted:  # CEC only toggles
            await self._command("audio", {"action": "mute"})
            self._assumed_muted = mute
            self.async_write_ha_state()
