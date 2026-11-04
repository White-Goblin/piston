"""Desktop mode switch.

The plugin backend is a system service, so it keeps running (and keeps answering Home Assistant) in desktop mode.
The device reports which session is active; on switches to desktop, off returns to game mode.
"""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .entity import DeckyEntity


# The device runs commands one at a time anyway; one call at a time is the natural limit for a mode switch.
PARALLEL_UPDATES = 1

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DesktopModeSwitch(entry.runtime_data), AutoUpdateSwitch(entry.runtime_data)])


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
