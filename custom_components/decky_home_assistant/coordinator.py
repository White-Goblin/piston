"""Holds the device's state groups, connection and power state, and notifies entities."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from .client import CommandError, DeckyClient, DeviceOffline, VersionSkew
from .const import CONF_MAC, DEFAULT_OPTIONS, OPT_SENSOR_INTERVAL

_LOGGER = logging.getLogger(__name__)

OFF_TIMEOUT = 60  # seconds without a connection (and no sleep announcement) before the device counts as off


class DeckyCoordinator:
    """`power` is "on" (connected), "standby" (announced sleep, then disconnected) or "off" (gone silently)."""

    def __init__(self, hass, entry, *, off_timeout: float = OFF_TIMEOUT,
                 persist_mac: Callable[[str], None] | None = None):
        self.hass, self.entry = hass, entry
        self.client: DeckyClient | None = None
        self.pip = None  # PipRelay, set up with the entry
        self.groups: dict[str, dict] = {}
        self.connected = False
        self.sleeping = False
        self.power = "off"
        self.problem: str | None = None
        self._off_timeout = off_timeout
        self._off_handle: asyncio.TimerHandle | None = None
        self._listeners: list[Callable[[], None]] = []
        self._event_listeners: dict[str, list[Callable[[dict], None]]] = {}
        self._persist_mac = persist_mac
        self._start_off_timer()  # never seen yet: becomes "off" unless it connects first

    @property
    def online(self) -> bool:
        """Reachable and awake. A suspended device may keep its TCP connection for a while, so `connected`
        alone would show stale data as live."""
        return self.connected and not self.sleeping

    # --- listeners -----------------------------------------------------------

    def async_add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None

    def async_add_event_listener(self, name: str, callback: Callable[[dict], None]) -> Callable[[], None]:
        self._event_listeners.setdefault(name, []).append(callback)
        return lambda: self._event_listeners[name].remove(callback) if callback in self._event_listeners.get(name, []) else None

    def _notify(self) -> None:
        for callback in list(self._listeners):
            callback()

    # --- input from the client ----------------------------------------------

    def on_state(self, name: str, data: dict) -> None:
        self.groups[name] = data
        if name == "device" and data.get("mac") and data["mac"] != self.entry.data.get(CONF_MAC) and self._persist_mac:
            self._persist_mac(data["mac"])
        self._notify()

    def on_event(self, name: str, data: dict) -> None:
        if name == "going_to_sleep":
            # The connection may well survive a short suspend, so do not wait for it to drop
            self.sleeping = True
            self.power = "standby"
            self._cancel_off_timer()
            self._notify()
        elif name == "resumed":
            self.sleeping = False
            if self.connected:
                self.power = "on"
                self._notify()
        for callback in list(self._event_listeners.get(name, [])):
            callback(data)

    def on_connection(self, up: bool) -> None:
        self.connected = up
        self._cancel_off_timer()
        if up:
            self.sleeping = False
            self.problem = None
            self.power = "on"
            if self.client is not None:
                asyncio.get_running_loop().create_task(self._apply_options())
        elif self.sleeping:
            self.power = "standby"
        else:
            self._start_off_timer()  # a silent drop: keep the last state until the timeout
        self._notify()

    def on_problem(self, error: Exception) -> None:
        self.problem = "version_skew_" + error.peer if isinstance(error, VersionSkew) else "auth_failed"
        self._notify()

    # --- power state timer ---------------------------------------------------

    def _start_off_timer(self) -> None:
        self._cancel_off_timer()
        self._off_handle = asyncio.get_event_loop().call_later(self._off_timeout, self._timed_out)

    def _cancel_off_timer(self) -> None:
        if self._off_handle is not None:
            self._off_handle.cancel()
            self._off_handle = None

    def _timed_out(self) -> None:
        self._off_handle = None
        if not self.connected and not self.sleeping:
            self.power = "off"
            self._notify()

    def shutdown(self) -> None:
        self._cancel_off_timer()

    # --- output --------------------------------------------------------------

    @property
    def options(self) -> dict:
        return {**DEFAULT_OPTIONS, **self.entry.options}

    async def _apply_options(self) -> None:
        """Push options the device must know about (sensor interval) after every connect or change."""
        try:
            await self.client.command("set_sensor_interval", {"seconds": self.options[OPT_SENSOR_INTERVAL]})
        except (DeviceOffline, CommandError) as e:
            _LOGGER.debug("could not set the sensor interval: %s", e)

    def async_update_listeners(self) -> None:
        self._notify()

    async def async_apply_options(self) -> None:
        if self.connected:
            await self._apply_options()

    async def async_command(self, name: str, data: dict | None = None) -> dict[str, Any]:
        """Run a command; raises DeviceOffline / CommandError (entities translate them for the user)."""
        if self.client is None:
            raise DeviceOffline("the device is offline")
        return await self.client.command(name, data)
