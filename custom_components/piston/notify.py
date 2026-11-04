"""Notify entity and the show_toast action: Steam toasts on the device."""
from __future__ import annotations

from homeassistant.components.notify import NotifyEntity
from homeassistant.helpers import entity_platform

from ._compat import vol
from .entity import DeckyEntity

# Toasts are shown one after another on the device.
PARALLEL_UPDATES = 1

ICONS = ["bell", "bolt", "camera", "car", "door", "fire", "home", "light", "lock", "motion", "temperature", "warning", "water"]
MAX_SECONDS = 600
DEFAULT_SECONDS = 5

SHOW_TOAST_SCHEMA = {
    vol.Required("message"): str,
    vol.Optional("title"): str,
    vol.Optional("duration", default=DEFAULT_SECONDS): vol.All(vol.Coerce(float), vol.Range(min=0, max=MAX_SECONDS)),
    vol.Optional("icon"): vol.In(ICONS),
    vol.Optional("silent", default=False): bool,
    vol.Optional("id"): str,
}


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([DeckyNotify(entry.runtime_data)])
    entity_platform.async_get_current_platform().async_register_entity_service("show_toast", SHOW_TOAST_SCHEMA, "async_show_toast")


class DeckyNotify(DeckyEntity, NotifyEntity):
    _attr_translation_key = "toast"

    def __init__(self, coordinator):
        super().__init__(coordinator, "notify")

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        await self.async_show_toast(message=message, title=title)

    async def async_show_toast(self, message: str, title: str | None = None, duration: float = DEFAULT_SECONDS,
                               icon: str | None = None, silent: bool = False, id: str | None = None) -> None:
        data = {"message": message, "duration": round(duration * 1000), "silent": silent}
        for key, value in (("title", title), ("icon", icon), ("id", id)):
            if value:
                data[key] = value
        await self._command("toast", data)
