"""Plugin update entity: the plugin on the device against this integration's version, with an Install button.

The integration's version is the version the plugin should run (see update_logic.py and docs/protocol.md).
"""
from __future__ import annotations

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.exceptions import HomeAssistantError

from . import update_logic
from .client import DeviceOffline
from .entity import DeckyEntity, command_failure

PARALLEL_UPDATES = 1  # an install is one action at a time


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PluginUpdate(entry.runtime_data)])


class PluginUpdate(DeckyEntity, UpdateEntity):
    _attr_translation_key = "plugin"
    # The install runs for a minute or more on the device after the button press, so progress comes from the device
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS

    def __init__(self, coordinator):
        super().__init__(coordinator, "plugin_update")

    @property
    def available(self) -> bool:
        # Also while only the update lane is open (a protocol mismatch): this is the entity that fixes the mismatch
        return self.coordinator.link_up

    @property
    def installed_version(self) -> str | None:
        return self.coordinator.plugin_version

    @property
    def latest_version(self) -> str | None:
        coordinator = self.coordinator
        if coordinator.update_state in ("no_lane", "predates_lane") or coordinator.target_version is None:
            return coordinator.plugin_version  # nothing this integration can do: not shown as an available update
        return coordinator.target_version

    @property
    def in_progress(self) -> bool:
        return self.coordinator.update_in_progress

    @property
    def release_url(self) -> str | None:
        target = self.coordinator.target_version
        return update_logic.release_url(target) if target else None

    @property
    def release_summary(self) -> str | None:
        """What the user has to do on the device while the update waits (Decky Loader asks before it installs)."""
        state = self.coordinator.update_state
        if state == "waiting_for_confirmation":
            return "Approve the install in the Decky Loader dialog on the device."
        if state == "waiting_for_ui":
            return "Switch the device to game mode, then approve the install in the Decky Loader dialog."
        return None

    @property
    def extra_state_attributes(self) -> dict:
        coordinator = self.coordinator
        attributes = {"update_state": coordinator.update_state}
        if coordinator.update_latch:
            attributes["last_failure"] = dict(coordinator.update_latch)
        return attributes

    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        try:
            await self.coordinator.async_install_update()
        except DeviceOffline as e:
            raise command_failure(e) from None
        if self.coordinator.update_state == "cannot_determine":
            raise HomeAssistantError("Could not read the checksum of the release from GitHub, so nothing was sent to the device.")
