"""Sensors: system stats from the `sensors` group, the Steam screen, and controller batteries."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory

from .entity import DeckyEntity, controller_labels, track_new_entities
from .proto import sensors as table

# Read-only and fed by pushes: nothing to limit.
PARALLEL_UPDATES = 0

SCREENS = ["game", "steam_over_game", "steam", "desktop"]


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    async_add_entities([SteamScreenSensor(coordinator), PipStateSensor(coordinator)])

    def build() -> dict:
        keys = coordinator.groups.get("sensors", {})
        return {k: DeckySensor(coordinator, k) for k in keys if table.describe(k)}

    entry.async_on_unload(track_new_entities(coordinator, async_add_entities, build))
    entry.async_on_unload(track_new_entities(coordinator, async_add_entities,
                                             lambda: {"sendspin": SendspinStatusSensor(coordinator)} if coordinator.sendspin else {}))
    entry.async_on_unload(track_new_entities(coordinator, async_add_entities, lambda: {
        serial: ControllerBatterySensor(coordinator, serial, label)
        for serial, label in controller_labels(coordinator).items()}))


class DeckySensor(DeckyEntity, SensorEntity):
    """One field of the `sensors` group, described by the shared table."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, key: str):
        super().__init__(coordinator, f"sensor_{key}")
        self._key = key
        name, attrs = table.describe(key)
        self._attr_name = name
        self._attr_device_class = attrs.get("device_class")
        self._attr_native_unit_of_measurement = attrs.get("unit_of_measurement")
        self._attr_state_class = attrs.get("state_class")
        self._attr_icon = attrs.get("icon")

    @property
    def available(self) -> bool:
        return (self.coordinator.online and "sensors" not in self.coordinator.restored
                and self._key in self.coordinator.groups.get("sensors", {}))

    @property
    def native_value(self):
        return self.coordinator.groups.get("sensors", {}).get(self._key)


class SteamScreenSensor(DeckyEntity, SensorEntity):
    """What is in front: the game, Steam's interface over the game, or Steam alone."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SCREENS
    _attr_translation_key = "steam_screen"

    def __init__(self, coordinator):
        super().__init__(coordinator, "steam_screen")

    @property
    def available(self) -> bool:
        return self.coordinator.online and "screen" in self.coordinator.groups.get("apps", {})

    @property
    def native_value(self):
        return self.coordinator.groups.get("apps", {}).get("screen")


class ControllerBatterySensor(DeckyEntity, SensorEntity):
    """Battery of one controller. Unavailable while the controller is not connected."""

    _attr_device_class = "battery"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = "measurement"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, serial: str, label: str):
        super().__init__(coordinator, f"controller_{serial}_battery")
        self._serial = serial
        self._attr_name = f"{label} battery"

    @property
    def _controller(self):
        return self.coordinator.groups.get("controllers", {}).get("connected", {}).get(self._serial)

    @property
    def available(self) -> bool:
        return self.coordinator.online and self._controller is not None

    @property
    def native_value(self):
        return self._controller["battery"] if self._controller else None

    @property
    def extra_state_attributes(self) -> dict:
        c = self._controller or {}
        return {"stale": c.get("stale"), "updated": c.get("updated"), "wireless": c.get("wireless")}


PIP_STATES = ["idle", "loading", "connecting", "negotiating", "playing", "error"]


class PipStateSensor(DeckyEntity, SensorEntity):
    """What the picture-in-picture overlay is doing."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PIP_STATES
    _attr_translation_key = "pip_state"

    def __init__(self, coordinator):
        super().__init__(coordinator, "pip_state")

    @property
    def available(self) -> bool:
        return self.coordinator.online and "pip" in self.coordinator.groups

    @property
    def native_value(self):
        state = self.coordinator.groups.get("pip", {}).get("state", "hidden")
        return "idle" if state == "hidden" else state if state in PIP_STATES else "error"

    @property
    def extra_state_attributes(self) -> dict:
        pip = self.coordinator.groups.get("pip", {})
        return {"camera": pip.get("entity_id") or None, "message": pip.get("message") or None}


SENDSPIN_STATES = ["disabled", "waiting", "connected", "streaming", "error"]


class SendspinStatusSensor(DeckyEntity, SensorEntity):
    """What the device's Sendspin player is doing: off, waiting for a server, connected, or streaming."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SENDSPIN_STATES
    _attr_translation_key = "sendspin_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "sendspin_status")

    @property
    def available(self) -> bool:
        return self.coordinator.online and self.coordinator.sendspin is not None

    @property
    def native_value(self):
        status = (self.coordinator.sendspin or {}).get("status")
        return status if status in SENDSPIN_STATES else "error"  # a status this version does not know is shown as a problem

    @property
    def extra_state_attributes(self) -> dict:
        group = self.coordinator.sendspin or {}
        return {"server_name": group.get("server_name"), "port": group.get("port"), "error": group.get("error"),
                "player_name": group.get("name")}
