"""PiP camera select and the show_pip / hide_pip actions."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_platform

from ._compat import vol
from .client import CommandError, DeviceOffline
from .const import (
    OPT_PIP_CAMERAS, OPT_PIP_DURATION, OPT_PIP_MUTE, OPT_PIP_POSITION, OPT_PIP_SIZE, PIP_DURATIONS, PIP_POSITIONS,
    PIP_SIZES,
)
from .entity import DeckyEntity, command_failure

# One PiP camera change at a time.
PARALLEL_UPDATES = 1

OFF = "Off"
ACTIVE_STATES = ("loading", "connecting", "negotiating", "playing")

SHOW_PIP_SCHEMA = {
    vol.Required("camera"): str,
    vol.Optional("position"): vol.In(PIP_POSITIONS),
    vol.Optional("size"): vol.In(tuple(PIP_SIZES)),
    vol.Optional("duration"): vol.All(vol.Coerce(int), vol.In(PIP_DURATIONS)),
    vol.Optional("mute"): bool,
}


def pip_options(options: dict, overrides: dict | None = None) -> dict:
    """The per-call settings sent to the device: the integration's options with any overrides applied."""
    o = overrides or {}
    duration = o["duration"] if o.get("duration") is not None else options[OPT_PIP_DURATION]
    return {
        "position": o.get("position") or options[OPT_PIP_POSITION],
        "size": PIP_SIZES[o.get("size") or options[OPT_PIP_SIZE]],
        "duration": int(duration) * 1000,
        "mute": options[OPT_PIP_MUTE] if o.get("mute") is None else bool(o["mute"]),
    }


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PipCameraSelect(entry.runtime_data)])
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service("show_pip", SHOW_PIP_SCHEMA, "async_show_pip")
    platform.async_register_entity_service("hide_pip", {}, "async_hide_pip")


class PipCameraSelect(DeckyEntity, SelectEntity):
    _attr_translation_key = "pip_camera"

    def __init__(self, coordinator):
        super().__init__(coordinator, "pip_camera")

    def _label(self, entity_id: str) -> str:
        state = self.coordinator.hass.states.get(entity_id) if hasattr(self.coordinator.hass, "states") else None
        return (state.name if state is not None else None) or entity_id

    @property
    def _cameras(self) -> dict:
        """Option label -> camera entity id, for the cameras chosen in the integration options."""
        ids = self.coordinator.options[OPT_PIP_CAMERAS]
        labels = [self._label(i) for i in ids]
        return {(label if labels.count(label) == 1 and label != OFF else f"{label} ({i})"): i for label, i in zip(labels, ids)}

    @property
    def options(self) -> list:
        return [OFF, *self._cameras]

    @property
    def current_option(self) -> str:
        pip = self.coordinator.groups.get("pip", {})
        if pip.get("state") in ACTIVE_STATES:
            for label, entity_id in self._cameras.items():
                if entity_id == pip.get("entity_id"):
                    return label
        return OFF

    async def async_select_option(self, option: str) -> None:
        if option == OFF:
            await self.async_hide_pip()
            return
        camera = self._cameras.get(option)
        if camera is None:
            raise ServiceValidationError(f"{option} is not one of the PiP cameras (choose cameras in the integration options)")
        await self.async_show_pip(camera=camera)

    async def async_show_pip(self, camera: str, position=None, size=None, duration=None, mute=None) -> None:
        overrides = {"position": position, "size": size, "duration": duration, "mute": mute}
        try:
            await self.coordinator.pip.async_show(camera, pip_options(self.coordinator.options, overrides))
        except (DeviceOffline, CommandError) as e:
            raise command_failure(e) from None

    async def async_hide_pip(self) -> None:
        try:
            await self.coordinator.pip.async_hide()
        except (DeviceOffline, CommandError) as e:
            raise command_failure(e) from None
