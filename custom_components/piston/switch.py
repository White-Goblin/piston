"""Desktop mode switch.

The plugin backend is a system service, so it keeps running (and keeps answering Home Assistant) in desktop mode.
The device reports which session is active; on switches to desktop, off returns to game mode.
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .client import CommandError, DeviceOffline
from .entity import DeckyEntity, command_failure, track_new_entities


# The device runs commands one at a time anyway; one call at a time is the natural limit for a mode switch.
PARALLEL_UPDATES = 1

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities([DesktopModeSwitch(coordinator), AutoUpdateSwitch(coordinator)])
    # Only for devices that report a Sendspin player (an older plugin has none)
    entry.async_on_unload(track_new_entities(coordinator, async_add_entities,
                                             lambda: {"sendspin": SendspinSwitch(coordinator)} if coordinator.sendspin else {}))


class DesktopModeSwitch(DeckyEntity, SwitchEntity):
    _attr_translation_key = "desktop_mode"

    def __init__(self, coordinator):
        super().__init__(coordinator, "desktop_mode")

    @property
    def available(self) -> bool:
        return self.coordinator.online and self.coordinator.groups.get("device", {}).get("session") in ("game", "desktop")

    @property
    def is_on(self) -> bool:
        return self.coordinator.groups.get("device", {}).get("session") == "desktop"

    async def async_turn_on(self, **kwargs) -> None:
        await self._command("power", {"action": "desktop"})

    async def async_turn_off(self, **kwargs) -> None:
        await self._command("power", {"action": "game"})


class AutoUpdateSwitch(DeckyEntity, SwitchEntity):
    """Whether this integration updates the plugin on the device by itself when the versions differ (on by default)."""

    _attr_translation_key = "auto_update"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator):
        super().__init__(coordinator, "auto_update")

    @property
    def available(self) -> bool:
        return True  # a setting of the integration: it can be changed while the device is away

    @property
    def is_on(self) -> bool:
        return self.coordinator.auto_update

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.set_auto_update(True)

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.set_auto_update(False)


class SendspinSwitch(DeckyEntity, SwitchEntity):
    """Whether the device's Sendspin player (Music Assistant and TTS audio in the background) is on. The setting lives on
    the device; the state shown is what the device reports, never a guess."""

    _attr_translation_key = "sendspin_player"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator):
        super().__init__(coordinator, "sendspin_player")

    @property
    def available(self) -> bool:
        return self.coordinator.online and self.coordinator.sendspin is not None

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.sendspin or {}).get("enabled"))

    async def _set(self, enabled: bool) -> None:
        try:
            await self.coordinator.async_set_sendspin(enabled)
        except (DeviceOffline, CommandError) as e:
            raise command_failure(e) from None

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)
