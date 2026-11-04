"""Non-volatile state of one device entry: what is worth knowing again after Home Assistant restarts.

Only structure and stable facts are kept, never values that may be wrong by the time they are read back, so a restored
entity can exist at startup without showing a stale reading as live:

| group       | kept                                                      | not kept (volatile)                       |
|-------------|-----------------------------------------------------------|-------------------------------------------|
| device      | device_id, name, mac, broadcast, protocol, wol            | session (game / desktop)                  |
| apps        | apps (the offered list)                                   | running, appid, name, screen, playback    |
| audio       | mode                                                      | volume, muted                             |
| controllers | seen (serial and name of every controller ever seen)      | connected (levels, charging)              |
| sensors     | keys (the names of the readings, as `{"keys": [...]}`)    | the readings                              |
| pip         | nothing                                                   | everything                                |
| media       | nothing                                                   | everything (what is playing is live)      |
| update      | plugin_version, auto_update, latch (last failed update)   | nothing (all of it is stable)             |

The file is `.storage/piston.<entry_id>`. It also records `last_connected`, the last moment there was a
connection to the device (the unreachable-too-long repair issue is measured from it).

Home Assistant's `Store.async_delay_save` pushes the write out to `delay` seconds after the LAST call (the earlier timer
fires, finds a later write time, and reschedules), so a change that happens every few seconds would postpone the write for ever. Saves are therefore requested only when the stable data actually changes (sensor
readings are not part of it), and a pending save is written when Home Assistant stops. A group that has not been
reported yet in this run keeps its previously stored value, so a restart is never able to overwrite known structure
with an empty snapshot.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Callable

from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY_SECONDS = 30

# group name -> the keys of that group that are kept
_PLAIN_FIELDS = {
    "device": ("device_id", "name", "mac", "broadcast", "protocol", "wol"),
    "apps": ("apps",),
    "audio": ("mode",),
    "controllers": ("seen",),
}


def stable_subset(groups: dict) -> dict:
    """The part of the live state groups worth storing (see the table above); values are copied."""
    out = {}
    for name, fields in _PLAIN_FIELDS.items():
        group = groups.get(name)
        if isinstance(group, dict):
            kept = {k: copy.deepcopy(group[k]) for k in fields if k in group}
            if kept:
                out[name] = kept
    sensors = groups.get("sensors")
    if isinstance(sensors, dict) and sensors:
        out["sensors"] = {"keys": sorted(str(k) for k in sensors)}
    return out


def restored_groups(saved: dict) -> dict:
    """The stored form back in the shape of live groups (sensor keys become keys with no value yet)."""
    out = {name: copy.deepcopy(group) for name, group in saved.items() if name != "sensors" and isinstance(group, dict)}
    keys = saved.get("sensors", {}).get("keys") if isinstance(saved.get("sensors"), dict) else None
    if isinstance(keys, list) and keys:
        out["sensors"] = {str(k): None for k in keys}
    return out


class _Storage(Store):
    """Version 1 is the first format; a newer major version (a downgrade) is refused and treated as empty."""

    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        if old_major_version < STORAGE_VERSION:
            return {}  # nothing older than version 1 exists
        raise NotImplementedError(f"storage version {old_major_version} is newer than this integration understands")


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _parse(text) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment and moment.tzinfo is None else moment


def _clean_update(value) -> dict:
    """The stored plugin-update facts, with anything of the wrong shape dropped."""
    out: dict = {}
    if not isinstance(value, dict):
        return out
    if isinstance(value.get("plugin_version"), str):
        out["plugin_version"] = value["plugin_version"]
    if isinstance(value.get("auto_update"), bool):
        out["auto_update"] = value["auto_update"]
    latch = value.get("latch")
    if isinstance(latch, dict) and isinstance(latch.get("version"), str) and isinstance(latch.get("reason"), str):
        out["latch"] = {"version": latch["version"], "reason": latch["reason"]}
    return out


class DeckyStore:
    """`current()` returns (live groups, last_connected, connected) for this entry."""

    def __init__(self, hass, entry_id: str, current: Callable[[], tuple[dict, datetime | None, bool]], *,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), store: Store | None = None,
                 current_update: Callable[[], dict] | None = None):
        self._store = store or _Storage(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self._current, self._now = current, now
        self._stored: dict = {}                 # groups read from the file (or last written)
        self._stored_last_connected: datetime | None = None
        self._requested = None                  # what the last save request covered, to skip identical ones
        self._current_update = current_update or (lambda: {})
        self._stored_update: dict = {}

    # --- reading ---------------------------------------------------------------

    async def async_load(self) -> dict:
        """{"groups": {...}, "last_connected": datetime | None}; empty when there is no usable file (logged once)."""
        try:
            data = await self._store.async_load()
        except Exception as e:  # noqa: BLE001 - corrupt file or a newer storage version
            _LOGGER.warning("Ignoring the stored device state, it cannot be read (%s)", e)
            return {"groups": {}, "last_connected": None, "update": {}}
        if not data:  # no file yet, or an older version that held nothing
            return {"groups": {}, "last_connected": None, "update": {}}
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, dict) or not all(isinstance(g, dict) for g in groups.values()):
            _LOGGER.warning("Ignoring the stored device state, it has an unexpected shape")
            return {"groups": {}, "last_connected": None, "update": {}}
        self._stored = copy.deepcopy(groups)
        self._stored_update = _clean_update(data.get("update"))
        self._stored_last_connected = _parse(data.get("last_connected"))
        self._requested = self._snapshot()
        return {"groups": copy.deepcopy(groups), "last_connected": self._stored_last_connected,
                "update": copy.deepcopy(self._stored_update)}

    # --- writing ---------------------------------------------------------------

    def _merged_groups(self) -> dict:
        live, _, _ = self._current()
        return {**self._stored, **stable_subset(live)}   # a group not reported yet in this run keeps its stored value

    def _last_connected(self) -> datetime | None:
        _, last, _ = self._current()
        return last or self._stored_last_connected

    def _snapshot(self) -> tuple:
        return (self._merged_groups(), _iso(self._last_connected()), _clean_update(self._current_update()))

    def _data(self) -> dict:
        """What is written: the merged groups, and `last_connected` as of now while the device is connected."""
        _, _, connected = self._current()
        last = self._now() if connected else self._last_connected()
        return {"saved_at": _iso(self._now()), "last_connected": _iso(last), "groups": self._merged_groups(),
                "update": _clean_update(self._current_update())}

    def note_changed(self) -> None:
        """Called on every coordinator change: schedule a delayed save only if the stored data would differ."""
        snapshot = self._snapshot()
        if snapshot == self._requested:
            return
        self._requested = snapshot
        self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    async def async_save_now(self) -> None:
        data = self._data()
        self._requested = self._snapshot()
        await self._store.async_save(data)

    @staticmethod
    async def async_remove_file(hass, entry_id: str) -> None:
        """Delete the file of an entry that is being removed (its coordinator no longer exists)."""
        await _Storage(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}").async_remove()
