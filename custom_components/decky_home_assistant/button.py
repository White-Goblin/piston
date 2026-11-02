"""Restart buttons."""
from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity

from .entity import DeckyEntity

BUTTONS = (
    ("restart_device", "restart"),
    ("restart_steam", "restart_steam"),
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DeckyPowerButton(entry.runtime_data, key, action) for key, action in BUTTONS])


class DeckyPowerButton(DeckyEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.RESTART

    def __init__(self, coordinator, key: str, action: str):
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._action = action

    async def async_press(self) -> None:
        await self._command("power", {"action": self._action})
