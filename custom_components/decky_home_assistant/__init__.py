"""Decky Home Assistant: control a SteamOS / Decky Loader device."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import config_flow
from .client import DeckyClient, VersionSkew, send_unpair
from .const import CONF_DEVICE_ID, CONF_HOST, CONF_MAC, CONF_NAME, CONF_PORT, CONF_SECRET, DEFAULT_PORT, DOMAIN, PLATFORMS
from .coordinator import DeckyCoordinator
from .discovery import resolve_address
from .pip import PipRelay

_LOGGER = logging.getLogger(__name__)


def _issue_id(entry: ConfigEntry, kind: str) -> str:
    return f"{kind}_{entry.data[CONF_DEVICE_ID]}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data

    def persist_mac(mac: str) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MAC: mac})

    coordinator = DeckyCoordinator(hass, entry, persist_mac=persist_mac)

    def on_connection(up: bool) -> None:
        coordinator.on_connection(up)
        if up:
            for kind in ("version_skew_old", "version_skew_new"):
                ir.async_delete_issue(hass, DOMAIN, _issue_id(entry, kind))

    def on_problem(error: Exception) -> None:
        coordinator.on_problem(error)
        if isinstance(error, VersionSkew):
            ir.async_create_issue(
                hass, DOMAIN, _issue_id(entry, f"version_skew_{error.peer}"), is_fixable=False,
                severity=ir.IssueSeverity.ERROR, translation_key=f"version_skew_{error.peer}",
                translation_placeholders={"name": data.get(CONF_NAME) or "the device"})
        else:
            entry.async_start_reauth(hass)

    client = DeckyClient(
        config_flow.make_transport(hass), data[CONF_HOST], data.get(CONF_PORT, DEFAULT_PORT), data[CONF_DEVICE_ID],
        bytes.fromhex(data[CONF_SECRET]), on_state=coordinator.on_state, on_event=coordinator.on_event,
        on_connection=on_connection, on_problem=on_problem,
        resolver=lambda: resolve_address(hass, data[CONF_DEVICE_ID]))
    coordinator.client = client
    coordinator.pip = PipRelay(hass, coordinator)
    coordinator.pip.start()
    entry.runtime_data = coordinator
    await client.start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    entry.runtime_data.async_update_listeners()  # entities that read options (the PiP camera list)
    await entry.runtime_data.async_apply_options()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: DeckyCoordinator = entry.runtime_data
        coordinator.pip.stop()
        coordinator.shutdown()
        await coordinator.client.stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The device was removed in Home Assistant: tell it to forget this pairing (best effort)."""
    data = entry.data
    try:
        await send_unpair(config_flow.make_transport(hass), data[CONF_HOST], data.get(CONF_PORT, DEFAULT_PORT),
                          data[CONF_DEVICE_ID], bytes.fromhex(data[CONF_SECRET]))
    except Exception as e:  # noqa: BLE001 - offline or already reset: use Reset pairing on the device
        _LOGGER.warning("Could not tell the device to unpair (%s). Use Reset pairing in its plugin panel.", e)
    for kind in ("version_skew_old", "version_skew_new"):
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry, kind))
