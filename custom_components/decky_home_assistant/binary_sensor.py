"""Connectivity: whether the plugin on the device is connected."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .entity import DeckyEntity, controller_labels, track_new_entities


# Read-only and fed by pushes: nothing to limit.
PARALLEL_UPDATES = 0

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities([ConnectivitySensor(coordinator)])
    entry.async_on_unload(track_new_entities(coordinator, async_add_entities, lambda: {
        serial: ControllerChargingSensor(coordinator, serial, label)
        for serial, label in controller_labels(coordinator).items()}))


class ConnectivitySensor(DeckyEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "connectivity"

    def __init__(self, coordinator):
        super().__init__(coordinator, "connectivity")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.online

    @property
    def extra_state_attributes(self) -> dict:
        return {"power": self.coordinator.power, "problem": self.coordinator.problem}


class ControllerChargingSensor(DeckyEntity, BinarySensorEntity):
    """Whether one controller is charging; unknown when the device cannot tell (Bluetooth pads, stale readings)."""

    _attr_device_class = "battery_charging"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, serial: str, label: str):
        super().__init__(coordinator, f"controller_{serial}_charging")
        self._serial = serial
        self._attr_name = f"{label} charging"

    @property
    def _controller(self):
        return self.coordinator.groups.get("controllers", {}).get("connected", {}).get(self._serial)

    @property
    def available(self) -> bool:
        return self.coordinator.online and self._controller is not None

    @property
    def is_on(self):
        return self._controller.get("charging") if self._controller else None
