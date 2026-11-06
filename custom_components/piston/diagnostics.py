"""Download diagnostics: a support summary of one device entry that is safe to post publicly.

Redacted: the pairing secret, the device's address, MAC and broadcast address, any IP address reading, controller serial
numbers, and the network connection name (it can be a Wi-Fi network name). The device id stays: it is not a secret (it is
announced on the local network) and support needs it to tell devices apart.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data

from .proto import PROTOCOL_VERSION

TO_REDACT = frozenset({"secret", "host", "mac", "broadcast", "ip", "serial", "connection", "ssid"})

# Belt and braces: an address that shows up inside free text (a device error message, a warning) is removed too
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC = re.compile(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b")


def _plain(value: Any) -> Any:
    """JSON-friendly copy: datetimes as ISO strings, sets as sorted lists, addresses inside text removed."""
    if isinstance(value, str):
        return _MAC.sub("**MAC**", _IPV4.sub("**IP**", value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _anonymous_controllers(groups: dict) -> dict:
    """The connected controllers are keyed by their serial number, which the key list cannot reach: number them instead."""
    controllers = groups.get("controllers")
    if not isinstance(controllers, dict) or not isinstance(controllers.get("connected"), dict):
        return groups
    renamed = {f"controller_{i}": value for i, value in enumerate(controllers["connected"].values(), 1)}
    return {**groups, "controllers": {**controllers, "connected": renamed}}


def _private_media(groups: dict) -> dict:
    """What is playing is viewing history: keep the shape of the media group (source, kind, progress, whether there is an
    image) and drop the text and the image id."""
    media = groups.get("media")
    if not isinstance(media, dict):
        return groups
    shown = {k: ("**REDACTED**" if k in ("title", "artist", "album") else v) for k, v in media.items() if k != "art"}
    if media.get("art"):
        shown["has_art"] = True
    return {**groups, "media": shown}


async def async_get_config_entry_diagnostics(hass, entry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    client = coordinator.client
    report = {
        "entry": {"unique_id": entry.unique_id, "data": dict(entry.data), "options": dict(entry.options)},
        "coordinator": {
            "power": coordinator.power, "connected": coordinator.connected, "sleeping": coordinator.sleeping,
            "problem": coordinator.problem, "last_connected": coordinator.last_connected,
            "restored_groups": coordinator.restored, "integration_protocol": PROTOCOL_VERSION,
            "device_protocol": coordinator.groups.get("device", {}).get("protocol"),
        },
        "client": {"probe_mode": getattr(client, "probe_mode", None), "failed_attempts": getattr(client, "failed_attempts", None),
                   "boot_id": getattr(client, "boot_id", None)},
        "input": {
            "capability": coordinator.input_capability, "status": coordinator.input_status,
            "frames_written": getattr(client, "input_frames", None), "events_dropped": getattr(client, "input_dropped", None),
        },
        "update": {
            "plugin_version": coordinator.plugin_version, "target_version": coordinator.target_version,
            "auto_update": coordinator.auto_update, "state": coordinator.update_state, "skewed": coordinator.skewed,
            "last_failure": coordinator.update_latch,
        },
        "groups": _private_media(_anonymous_controllers(coordinator.groups)),
    }
    return async_redact_data(_plain(report), TO_REDACT)
