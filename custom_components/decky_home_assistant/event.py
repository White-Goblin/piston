"""Event entity that fires when a toast is clicked on the device."""
from __future__ import annotations

from homeassistant.components.event import EventEntity

from .entity import DeckyEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ToastClickEvent(entry.runtime_data)])


class ToastClickEvent(DeckyEntity, EventEntity):
    _attr_translation_key = "toast_click"
    _attr_event_types = ["clicked"]

    def __init__(self, coordinator):
        super().__init__(coordinator, "toast_click")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_event_listener("toast_click", self._clicked))

    def _clicked(self, data: dict) -> None:
        self._trigger_event("clicked", {"id": data.get("id") or None, "clicked_at": data.get("at")})
        self.async_write_ha_state()
