"""Holds the device's state groups, connection and power state, and notifies entities."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .client import CommandError, DeckyClient, DeviceOffline, VersionSkew
from . import update_logic
from .const import CONF_MAC, CONF_NAME, DEFAULT_OPTIONS, OPT_SENSOR_INTERVAL

_LOGGER = logging.getLogger(__name__)

OFF_TIMEOUT = 60  # seconds without a connection (and no sleep announcement) before the device counts as off


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Groups that are live state: never restored from the Store, dropped when the device is not known to be awake
LIVE_GROUPS = ("media", "sendspin")


class DeckyCoordinator:
    """`power` is "on" (connected and awake), "standby" (announced sleep) or "off" (announced shutdown, or gone silently).

    `last_connected` is the last moment there was a connection to the device (set when one starts and when one ends),
    or None if there never was one since Home Assistant started."""

    def __init__(self, hass, entry, *, off_timeout: float = OFF_TIMEOUT,
                 persist_mac: Callable[[str], None] | None = None, now: Callable[[], datetime] = _utcnow,
                 target_version: str | None = None, fetch_text: Callable[[str], Awaitable[str]] | None = None):
        self.hass, self.entry = hass, entry
        self._now = now
        self._shut_down = False
        self.last_connected: datetime | None = None
        self._unavailable_logged = False
        self.client: DeckyClient | None = None
        self.pip = None  # PipRelay, set up with the entry
        self.wake_listener = None  # WakeListener, set up with the entry
        self.store = None  # DeckyStore, set up with the entry
        self.unreachable = None  # UnreachableMonitor, set up with the entry
        self.groups: dict[str, dict] = {}
        self.restored: set[str] = set()  # groups that came from the stored state of the last run and are not live yet
        self.connected = False
        self.sleeping = False
        self.power = "off"
        self.problem: str | None = None
        self._off_timeout = off_timeout
        self._off_handle: asyncio.TimerHandle | None = None
        self._listeners: list[Callable[[], None]] = []
        self._event_listeners: dict[str, list[Callable[[dict], None]]] = {}
        self._persist_mac = persist_mac
        # Plugin self-update (see update_logic.py): the integration's version is the version the plugin should run
        self.target_version = target_version
        self._fetch_text = fetch_text
        self.plugin_version: str | None = None      # from the device hello; remembered across restarts
        self.auto_update = True
        self.update_latch: dict | None = None       # {"version", "reason"} of the last failed update
        self.update_status: dict | None = None      # the last update_status frame {state, version, reason?}
        self.skewed: str | None = None              # "old" / "new" while only the update lane is open
        self.update_outcome: str | None = None      # last decide() outcome or "cannot_determine"
        self._expect_install = False                # an install was under way: the next hello must show the target
        self._sums_cache: dict = {}
        self._decide_task: asyncio.Task | None = None
        self._start_off_timer()  # never seen yet: becomes "off" unless it connects first

    @property
    def online(self) -> bool:
        """Reachable, awake and nothing wrong with the pairing or versions. A suspended device may keep its TCP
        connection for a while (and stays open briefly after a shutdown announcement), so `connected` alone would show stale
        data as live. Every entity except the media
        player is available only while this is true; the media player stays available while the device is merely
        asleep or off (turn_on must keep working) and goes unavailable only for a problem."""
        return self.connected and not self.sleeping and self.power == "on" and self.problem is None

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

    def restore(self, groups: dict, last_connected: datetime | None) -> None:
        """Seed the structure known from the last run (see store.py) so entities exist before the device connects.
        Live data always wins: a group that is already live is left alone, and the first live frame of a restored
        group replaces it and clears its flag. The power state is not restored: it starts as off until connected."""
        for name, data in groups.items():
            if name in LIVE_GROUPS:
                continue  # never restored (live state only)
            if name not in self.groups:
                self.groups[name] = data
                self.restored.add(name)
        if last_connected is not None and self.last_connected is None:
            self.last_connected = last_connected

    def on_state(self, name: str, data: dict) -> None:
        self.groups[name] = data
        self.restored.discard(name)
        if name == "device" and data.get("mac") and data["mac"] != self.entry.data.get(CONF_MAC) and self._persist_mac:
            self._persist_mac(data["mac"])
        self._notify()

    def _drop_media(self) -> None:
        """What is playing and the Sendspin player's status are live state: never stored or restored, and gone as soon as
        the device is not known to be awake and connected (the next connection's snapshot brings them back)."""
        for name in LIVE_GROUPS:
            self.groups.pop(name, None)
            self.restored.discard(name)

    @property
    def sendspin(self) -> dict | None:
        """The Sendspin player's status group, or None when the device does not report one (an older plugin, or it is
        asleep/off). Entities for it exist only for devices that report it."""
        return self.groups.get("sendspin")

    async def async_set_sendspin(self, enabled: bool) -> None:
        """Turn the device's Sendspin player on or off (the device pushes the new status itself)."""
        await self.async_command("sendspin_enable", {"enabled": bool(enabled)})

    def on_event(self, name: str, data: dict) -> None:
        if name == "going_to_sleep":
            # The connection may well survive a short suspend, so do not wait for it to drop
            self._drop_media()
            self.sleeping = True
            self._set_power("standby")
            self._cancel_off_timer()
            self._notify()
        elif name == "shutting_down":
            # Announced by the device itself: no need to guess from a dropped connection
            self._drop_media()
            self.sleeping = False
            self._set_power("off")
            self._cancel_off_timer()
            self._notify()
        elif name == "resumed":
            self.sleeping = False
            if self.connected:
                self._set_power("on")
                self._notify()
        for callback in list(self._event_listeners.get(name, [])):
            callback(data)

    def on_connection(self, up: bool) -> None:
        self.connected = up
        if not up:
            self._drop_media()
        self.last_connected = self._now()
        self._cancel_off_timer()
        if up:
            self.sleeping = False
            self.problem = None
            self._set_power("on")
            if self.client is not None:
                asyncio.get_running_loop().create_task(self._apply_options())
        elif self.sleeping:
            self._set_power("standby")
        elif self.power != "off":
            self._start_off_timer()  # a silent drop: keep the last state until the timeout
        self._notify()

    def on_hello(self, hello: dict, skew: str | None) -> None:
        """Called by the client after every authenticated connection, before it is announced as connected."""
        version = hello.get("plugin_version")
        self.plugin_version = version if isinstance(version, str) else None
        self.skewed = skew
        if self.target_version is not None:
            if self.plugin_version == self.target_version:
                self.update_latch = None
                self.update_status = None
            elif self._expect_install:
                self.update_latch = {"version": self.target_version, "reason": "version"}
        self._expect_install = False
        self._notify()
        self.schedule_update_check()

    def on_lane_closed(self) -> None:
        self.skewed = None
        self._notify()

    @property
    def link_up(self) -> bool:
        """An authenticated connection exists: the normal one, or the update lane under a protocol mismatch."""
        return self.connected or (self.skewed is not None and self.client is not None and self.client.lane_connected)

    def on_update_status(self, frame: dict) -> None:
        state = frame["state"]
        self.update_status = {"state": state, "version": frame.get("version", ""), "reason": frame.get("reason")}
        if state in ("downloading", "waiting_for_ui", "waiting_for_confirmation", "installing") and self.update_latch \
                and self.update_latch.get("reason") == "version":
            self.update_latch = None  # the device is working on it again (for example a dropped connection while the dialog waited)
        if state in ("waiting_for_confirmation", "installing"):
            self._expect_install = True
        elif state == "failed":
            self._expect_install = False
            self.update_latch = {"version": frame.get("version") or self.target_version or "", "reason": frame.get("reason") or "installer"}
        self._notify()

    @property
    def update_in_progress(self) -> bool:
        return self.update_status is not None and self.update_status["state"] in (
            "downloading", "waiting_for_ui", "waiting_for_confirmation", "installing")

    @property
    def update_needs_approval(self) -> bool:
        """The update is ready and waiting for the user: for the dialog of Decky Loader on the device (or for game mode)."""
        return self.update_status is not None and self.update_status["state"] in ("waiting_for_ui", "waiting_for_confirmation")

    @property
    def update_state(self) -> str:
        """For the update entity: waiting_for_ui, waiting_for_confirmation, in_progress, failed, cannot_determine, up_to_date, available, predates_lane, no_lane or unknown."""
        if self.update_needs_approval:
            return self.update_status["state"]
        if self.update_in_progress:
            return "in_progress"
        if self.update_latch and self.update_latch["version"] == self.target_version:
            return "failed"
        if self.update_outcome in (None, update_logic.SEND):
            return "unknown" if self.update_outcome is None else "available"
        return self.update_outcome

    def schedule_update_check(self, pressed: bool = False) -> None:
        """Decide in a task: the client announces the hello before its connection object is usable for sending."""
        if self._shut_down or self.client is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._decide_task = loop.create_task(self.async_check_update(pressed))

    async def async_check_update(self, pressed: bool = False) -> None:
        """Compare the device's plugin with the integration's version and send an update when the rules say so."""
        if self.target_version is None or self.update_in_progress:
            return
        outcome = update_logic.decide(self.plugin_version, self.target_version, auto=self.auto_update, pressed=pressed,
                                      latch=self.update_latch)
        self.update_outcome = outcome
        if outcome == update_logic.SEND:
            checksum = await update_logic.zip_checksum(self._fetch_text, self.target_version, self._sums_cache) \
                if self._fetch_text is not None else None
            if checksum is None:
                self.update_outcome = update_logic.CANNOT_DETERMINE
            else:
                try:
                    await self.client.send_update(self.target_version, update_logic.asset_url(self.target_version), checksum)
                    self.update_latch = None if pressed else self.update_latch
                except DeviceOffline:
                    if pressed:
                        raise  # a press must say it did not work; the automatic check just tries again at the next connection
        self._notify()

    async def async_install_update(self) -> None:
        """The Install button: send the update now, whatever the auto setting and an earlier failure say."""
        await self.async_check_update(pressed=True)

    def set_auto_update(self, value: bool) -> None:
        self.auto_update = value
        self._notify()
        if value:
            self.schedule_update_check()

    def update_facts(self) -> dict:
        """What the store keeps about updates."""
        facts: dict = {"auto_update": self.auto_update}
        if self.plugin_version is not None:
            facts["plugin_version"] = self.plugin_version
        if self.update_latch is not None:
            facts["latch"] = dict(self.update_latch)
        return facts

    def restore_update(self, facts: dict) -> None:
        if isinstance(facts.get("auto_update"), bool):
            self.auto_update = facts["auto_update"]
        if self.plugin_version is None and isinstance(facts.get("plugin_version"), str):
            self.plugin_version = facts["plugin_version"]
        if isinstance(facts.get("latch"), dict):
            self.update_latch = dict(facts["latch"])

    def on_problem(self, error: Exception) -> None:
        self.problem = "version_skew_" + error.peer if isinstance(error, VersionSkew) else "auth_failed"
        self._notify()

    # --- power state timer ---------------------------------------------------

    def _start_off_timer(self) -> None:
        self._cancel_off_timer()
        if self._shut_down:
            return  # stopping the client reports a lost connection after shutdown(); that must not start a timer again
        self._off_handle = asyncio.get_event_loop().call_later(self._off_timeout, self._timed_out)

    def _cancel_off_timer(self) -> None:
        if self._off_handle is not None:
            self._off_handle.cancel()
            self._off_handle = None

    def _timed_out(self) -> None:
        self._off_handle = None
        if not self.connected and not self.sleeping:
            self._set_power("off")
            self._notify()

    def _set_power(self, power: str) -> None:
        """Change the power state, logging once when the device goes away and once when it is back (not per attempt)."""
        self.power = power
        name = self.entry.data.get(CONF_NAME) or "The device"
        if power != "on" and not self._unavailable_logged:
            self._unavailable_logged = True
            _LOGGER.info("%s is unavailable (%s)", name, power)
        elif power == "on" and self._unavailable_logged:
            self._unavailable_logged = False
            _LOGGER.info("%s is back online", name)

    def shutdown(self) -> None:
        self._shut_down = True
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
