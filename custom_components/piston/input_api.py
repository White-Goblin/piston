"""The Home Assistant websocket API behind the remote card: `piston/input/subscribe` and `piston/input/send`.

The card sends pointer and keyboard events here; they go to the device as `input` frames (see docs/protocol.md). Only an
administrator may use it unless the entry's option "allow all users" is on, checked on every call."""
from __future__ import annotations

import logging

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from ._compat import vol
from .const import CONF_DEVICE_ID, DOMAIN, OPT_ALLOW_ALL_USERS
from .proto import messages as m

_LOGGER = logging.getLogger(__name__)

SUBSCRIBE = "piston/input/subscribe"
SEND = "piston/input/send"
_REGISTERED = f"{DOMAIN}_input_api_registered"


def entry_for(hass: HomeAssistant, device_id: str):
    """The loaded config entry for a Home Assistant device id (what the card holds) or a Piston device id; else None."""
    entries = {entry.entry_id: entry for entry in hass.config_entries.async_entries(DOMAIN)}
    device = dr.async_get(hass).async_get(device_id)
    owner = getattr(device, "config_entry_id", None)  # a device belongs to one config entry since Home Assistant 2026.10
    candidates = [entries[owner]] if owner in entries else []
    candidates += [entry for entry in entries.values() if entry.data.get(CONF_DEVICE_ID) == device_id]
    for entry in candidates:
        if getattr(entry, "runtime_data", None) is not None:
            return entry
    return None


def authorized(connection, entry) -> bool:
    user = connection.user
    return bool(user and user.is_admin) or bool(entry.options.get(OPT_ALLOW_ALL_USERS, False))


def _resolve(hass, connection, msg):
    """The entry for this call, or None after answering the error."""
    entry = entry_for(hass, msg["device_id"])
    if entry is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "no such Piston device")
        return None
    if not authorized(connection, entry):
        connection.send_error(msg["id"], websocket_api.ERR_UNAUTHORIZED, "remote input needs an administrator")
        return None
    return entry


@websocket_api.websocket_command({vol.Required("type"): SUBSCRIBE, vol.Required("device_id"): str})
@callback
def ws_subscribe(hass: HomeAssistant, connection, msg: dict) -> None:
    """Stream the input status (`ready`, `device_off`, ...) of a device; closing it lets go of any held input."""
    entry = _resolve(hass, connection, msg)
    if entry is None:
        return
    coordinator = entry.runtime_data
    msg_id = msg["id"]

    def send_status(status: str) -> None:
        connection.send_message(websocket_api.event_message(msg_id, {"status": status}))

    remove = coordinator.async_add_input_listener(send_status)

    def unsubscribe() -> None:
        remove()
        if coordinator.client is not None:
            coordinator.client.send_release()

    connection.subscriptions[msg_id] = unsubscribe
    connection.send_result(msg_id)
    send_status(coordinator.input_status)


@websocket_api.websocket_command(
    {vol.Required("type"): SEND, vol.Required("device_id"): str, vol.Required("events"): list})
@callback
def ws_send(hass: HomeAssistant, connection, msg: dict) -> None:
    """Forward input events to the device. Answers only a failure: the card does not wait for a reply."""
    entry = _resolve(hass, connection, msg)
    if entry is None:
        return
    try:
        events = m.input_events(msg["events"])
    except m.ProtocolError as e:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, e.message)
        return
    client = entry.runtime_data.client
    if client is not None:
        client.write_input(events)  # not connected: dropped, the subscription status already says why


def async_register(hass: HomeAssistant) -> None:
    """Register the two commands once per Home Assistant run (they stay registered when the last entry unloads)."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_send)
