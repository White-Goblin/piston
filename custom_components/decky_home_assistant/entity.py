"""Base class for the device's entities."""
from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity

from .client import CommandError, DeviceOffline
from .const import CONF_DEVICE_ID, CONF_NAME, DOMAIN
from .coordinator import DeckyCoordinator


class DeckyEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: DeckyCoordinator, key: str):
        self.coordinator = coordinator
        device_id = coordinator.entry.data[CONF_DEVICE_ID]
        self._attr_unique_id = f"{device_id}_{key}"
        info = {"identifiers": {(DOMAIN, device_id)}, "name": coordinator.entry.data.get(CONF_NAME) or "Decky device",
                "manufacturer": "SteamOS", "model": "Decky Loader device"}
        mac = coordinator.groups.get("device", {}).get("mac") or coordinator.entry.data.get("mac")
        if mac:
            info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
        self._attr_device_info = DeviceInfo(**info)

    @property
    def available(self) -> bool:
        return self.coordinator.online

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))

    async def _command(self, name: str, data: dict | None = None) -> dict:
        """Run a device command; failures become errors Home Assistant shows to the user."""
        try:
            return await self.coordinator.async_command(name, data)
        except DeviceOffline:
            raise HomeAssistantError("The device is offline") from None
        except CommandError as e:
            raise HomeAssistantError(e.message or e.code) from None


def track_new_entities(coordinator: DeckyCoordinator, async_add_entities, build) -> callable:
    """Add entities for keys that appear in the device's state over time (new sensors, new controllers).

    `build()` returns {key: entity} for everything currently in the state; only unseen keys are added.
    Returns the function that stops tracking."""
    known: set = set()

    def add_new() -> None:
        fresh = {k: e for k, e in build().items() if k not in known}
        if fresh:
            known.update(fresh)
            async_add_entities(list(fresh.values()))

    add_new()
    return coordinator.async_add_listener(add_new)


def controller_labels(coordinator: DeckyCoordinator) -> dict[str, str]:
    """serial -> display name for every controller ever seen or connected; same-named ones get their serial tail."""
    group = coordinator.groups.get("controllers", {})
    names = {c["serial"]: c["name"] for c in group.get("seen", [])}
    for serial, c in group.get("connected", {}).items():
        names.setdefault(serial, c.get("name", "Controller"))
    counts = list(names.values())
    return {s: (f"{n} {s[-4:].upper()}" if counts.count(n) > 1 else n) for s, n in names.items()}
