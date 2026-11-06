"""What the remote card may do right now, derived from the coordinator's state (pure, no Home Assistant imports)."""
from __future__ import annotations

from .proto import messages as m

READY = "ready"
DEVICE_OFF = "device_off"
UNREACHABLE = "unreachable"
DESKTOP = "desktop"
NO_CAPABILITY = "no_capability"
DISABLED = "disabled"
ERROR = "error"            # remote input is on but the device could not create its virtual input devices
SKEW = "skew"

STATUSES = (READY, DEVICE_OFF, UNREACHABLE, DESKTOP, NO_CAPABILITY, DISABLED, ERROR, SKEW)

UNSUPPORTED = "unsupported"
CAP_DISABLED = "disabled"
CAP_ENABLED = "enabled"


def capability(device_group: dict | None) -> str:
    """`unsupported` (a plugin that predates remote input), `disabled` (switched off on the device) or `enabled`."""
    if not isinstance(device_group, dict):
        return UNSUPPORTED
    try:
        field = m.device_input(device_group.get("input"))
    except m.ProtocolError:
        return UNSUPPORTED
    if field is None:
        return UNSUPPORTED
    return CAP_ENABLED if field["enabled"] else CAP_DISABLED


def input_status(*, power: str, connected: bool, sleeping: bool, skewed: str | None, problem: str | None,
                 device_group: dict | None) -> str:
    """One of STATUSES. Order matters: a version mismatch first, then whether the device is there at all, then what the
    plugin reports about remote input, then the session (desktop mode keeps the pointer and keys but not the overlay)."""
    if skewed is not None:
        return SKEW
    if not connected:
        if problem is not None:
            return UNREACHABLE
        return DEVICE_OFF if power in ("off", "standby") or sleeping else UNREACHABLE
    if sleeping or power != "on":
        return DEVICE_OFF
    cap = capability(device_group)
    if cap == UNSUPPORTED:
        return NO_CAPABILITY
    if cap == CAP_DISABLED:
        return DISABLED
    if device_group["input"].get("error"):
        return ERROR
    if isinstance(device_group, dict) and device_group.get("session") == "desktop":
        return DESKTOP
    return READY
