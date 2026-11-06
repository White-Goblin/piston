"""Piston: control a SteamOS / Decky Loader device."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import card, config_flow, input_api
from .client import DeckyClient, VersionSkew, send_unpair
from .const import CONF_DEVICE_ID, CONF_HOST, CONF_MAC, CONF_NAME, CONF_PORT, CONF_SECRET, DEFAULT_PORT, DOMAIN, PLATFORMS
from .coordinator import DeckyCoordinator
from .update_logic import REASON_TEXT, SUMS_TIMEOUT
from .discovery import WakeListener, resolve_address
from .pip import PipRelay
from .store import DeckyStore, restored_groups
from .unreachable import UnreachableMonitor, issue_id as unreachable_issue_id

_LOGGER = logging.getLogger(__name__)


def _issue_id(entry: ConfigEntry, kind: str) -> str:
    return f"{kind}_{entry.data[CONF_DEVICE_ID]}"


async def _integration_version(hass: HomeAssistant) -> str | None:
    """The installed integration's version (its manifest), which is the version the plugin should run."""
    try:
        from homeassistant.loader import async_get_integration
        return str((await async_get_integration(hass, DOMAIN)).version)
    except Exception:  # noqa: BLE001 - no version, no plugin updates
        _LOGGER.debug("could not read the integration version", exc_info=True)
        return None


def _text_fetcher(hass: HomeAssistant):
    async def fetch_text(url: str) -> str:
        import aiohttp  # Home Assistant always has it; the stdlib test suite does not

        session = async_get_clientsession(hass)
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=SUMS_TIMEOUT)) as response:
            response.raise_for_status()
            return await response.text()

    return fetch_text


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data
    input_api.async_register(hass)  # the remote card's websocket commands (once per run)
    await card.async_register_card(hass, await _integration_version(hass))  # and the card itself (once per run)

    def persist_mac(mac: str) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MAC: mac})

    def persist_address(host: str, port: int) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: host, CONF_PORT: port})

    coordinator = DeckyCoordinator(hass, entry, persist_mac=persist_mac, target_version=await _integration_version(hass),
                                   fetch_text=_text_fetcher(hass))
    store = DeckyStore(hass, entry.entry_id, lambda: (coordinator.groups, coordinator.last_connected, coordinator.connected),
                       current_update=coordinator.update_facts)
    coordinator.store = store
    entry.async_on_unload(coordinator.async_add_listener(store.note_changed))  # a delayed save, only when stable data changed

    async def save_on_stop(event) -> None:
        await store.async_save_now()  # fresh last_connected before Home Assistant goes down

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, save_on_stop))

    def on_connection(up: bool) -> None:
        coordinator.on_connection(up)
        if up:
            for kind in ("version_skew_old", "version_skew_new"):
                ir.async_delete_issue(hass, DOMAIN, _issue_id(entry, kind))

    shown_issues: dict[str, str] = {}   # kind -> the placeholders it was created with

    def sync_update_issues() -> None:
        """Repairs for the plugin update: one when an update failed, one when the plugin differs and nothing updates it by itself."""
        latch, target = coordinator.update_latch, coordinator.target_version
        failed = bool(latch) and latch["version"] == target and coordinator.plugin_version != target
        skewed_manual = (coordinator.skewed is not None and not coordinator.auto_update and not failed
                         and not coordinator.update_in_progress)
        name = data.get(CONF_NAME) or "the device"
        for kind, wanted, placeholders in (
                ("plugin_update_failed", failed, {"name": name, "version": (latch or {}).get("version", ""),
                                                  "reason": REASON_TEXT.get((latch or {}).get("reason", ""), "of an unknown reason")}),
                ("version_skew_updatable", skewed_manual, {"name": name}),
                ("plugin_update_confirm", coordinator.update_needs_approval, {"name": name})):
            if wanted and shown_issues.get(kind) != repr(placeholders):
                shown_issues[kind] = repr(placeholders)
                ir.async_create_issue(hass, DOMAIN, _issue_id(entry, kind), is_fixable=False, severity=ir.IssueSeverity.WARNING,
                                      translation_key=kind, translation_placeholders=placeholders)
            elif not wanted and kind in shown_issues:
                del shown_issues[kind]
                ir.async_delete_issue(hass, DOMAIN, _issue_id(entry, kind))

    entry.async_on_unload(coordinator.async_add_listener(sync_update_issues))

    def on_problem(error: Exception) -> None:
        coordinator.on_problem(error)
        if isinstance(error, VersionSkew) and error.updatable:
            pass  # the plugin can be updated from here: sync_update_issues decides whether anyone needs to be told
        elif isinstance(error, VersionSkew):
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
        on_hello=coordinator.on_hello, on_update_status=coordinator.on_update_status,
        on_lane_closed=coordinator.on_lane_closed,
        resolver=lambda: resolve_address(hass, data[CONF_DEVICE_ID]))
    coordinator.client = client
    saved = await store.async_load()  # before the client starts and before any entity is created
    coordinator.restore(restored_groups(saved["groups"]), saved["last_connected"])
    coordinator.restore_update(saved.get("update", {}))
    coordinator.pip = PipRelay(hass, coordinator)
    coordinator.pip.start()
    entry.runtime_data = coordinator
    coordinator.unreachable = UnreachableMonitor(hass, entry, coordinator, store.async_save_now)
    entry.async_on_unload(coordinator.unreachable.start())
    await client.start()
    coordinator.wake_listener = WakeListener(hass, data[CONF_DEVICE_ID], client, persist_address)
    await coordinator.wake_listener.start()
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
        await coordinator.store.async_save_now()  # while the connection state is still known
        coordinator.pip.stop()
        coordinator.shutdown()
        await coordinator.wake_listener.stop()
        await coordinator.client.stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The device was removed in Home Assistant: tell it to forget this pairing (best effort)."""
    data = entry.data
    await DeckyStore.async_remove_file(hass, entry.entry_id)
    ir.async_delete_issue(hass, DOMAIN, unreachable_issue_id(data[CONF_DEVICE_ID]))
    try:
        await send_unpair(config_flow.make_transport(hass), data[CONF_HOST], data.get(CONF_PORT, DEFAULT_PORT),
                          data[CONF_DEVICE_ID], bytes.fromhex(data[CONF_SECRET]))
    except Exception as e:  # noqa: BLE001 - offline or already reset: use Reset pairing on the device
        _LOGGER.warning("Could not tell the device to unpair (%s). Use Reset pairing in its plugin panel.", e)
    for kind in ("version_skew_old", "version_skew_new", "plugin_update_failed", "version_skew_updatable", "plugin_update_confirm"):
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry, kind))
