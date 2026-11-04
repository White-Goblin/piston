"""A repair issue for a device that has not been reachable for a long time.

Most of these devices are switched off or asleep much of the time, and that is normal, so the threshold is long (24 h)
and the issue is a warning that cannot be "fixed" from Home Assistant; it goes away by itself on the next connection.
The clock runs from the last moment there was a connection, which is stored (store.py), so a Home Assistant restart
does not reset it. A device that has never been seen since setup is measured from the setup.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from homeassistant.helpers import issue_registry as ir

from .const import CONF_DEVICE_ID, CONF_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

UNREACHABLE_AFTER = timedelta(hours=24)
CHECK_EVERY = timedelta(hours=1)


def issue_id(device_id: str) -> str:
    return f"device_unreachable_{device_id}"


def _track_interval(hass, action, interval):
    from homeassistant.helpers.event import async_track_time_interval

    return async_track_time_interval(hass, action, interval)


class UnreachableMonitor:
    """`refresh_stored` is awaited on every hourly check while connected, to keep the stored last-connected time fresh."""

    def __init__(self, hass, entry, coordinator, refresh_stored: Callable[[], Awaitable] | None = None, *,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), track_interval=_track_interval):
        self._hass, self._entry, self._coordinator = hass, entry, coordinator
        self._refresh_stored, self._now, self._track = refresh_stored, now, track_interval
        self._issue = issue_id(entry.data[CONF_DEVICE_ID])
        self._open = False
        self._started_at: datetime | None = None

    def start(self) -> Callable[[], None]:
        """Begin watching; returns the function that stops it."""
        self._started_at = self._now()
        unsub_listener = self._coordinator.async_add_listener(self._changed)
        unsub_timer = self._track(self._hass, self._tick, CHECK_EVERY)
        self.check()

        def stop() -> None:
            unsub_listener()
            unsub_timer()

        return stop

    def clear(self) -> None:
        if self._open:
            self._open = False
        ir.async_delete_issue(self._hass, DOMAIN, self._issue)

    def _changed(self) -> None:
        if self._coordinator.connected and self._open:
            self.clear()  # back: no need to wait for the next hourly check

    async def _tick(self, _now=None) -> None:
        self.check()
        if self._coordinator.connected and self._refresh_stored is not None:
            await self._refresh_stored()

    def check(self) -> None:
        """Create the issue when the device has been unreachable for over 24 h, remove it when connected."""
        if self._coordinator.connected:
            if self._open:
                self.clear()
            return
        since = self._coordinator.last_connected or self._started_at
        if self._open or since is None or self._now() - since <= UNREACHABLE_AFTER:
            return
        self._open = True
        _LOGGER.info("%s has not been reachable since %s", self._entry.data.get(CONF_NAME) or "The device", since.isoformat())
        ir.async_create_issue(
            self._hass, DOMAIN, self._issue, is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key="device_unreachable",
            translation_placeholders={"name": self._entry.data.get(CONF_NAME) or "the device"})
