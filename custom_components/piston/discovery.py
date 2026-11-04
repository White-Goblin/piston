"""Find a known device again: look it up by name, and watch for it to announce itself on mDNS.

The watcher is the fast path back after a wake. The device re-announces itself as soon as its network is up, which
lets the client connect at once instead of at the next probe; the probe loop in client.py stays the guaranteed path.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .const import SERVICE_TYPE

_LOGGER = logging.getLogger(__name__)


async def resolve_address(hass, device_id: str) -> tuple[str, int] | None:
    """Ask mDNS where the device is now; None when it does not answer."""
    try:
        from homeassistant.components import zeroconf
        from zeroconf.asyncio import AsyncServiceInfo

        aiozc = await zeroconf.async_get_async_instance(hass)
        info = AsyncServiceInfo(SERVICE_TYPE, f"{device_id}.{SERVICE_TYPE}")
        if await info.async_request(aiozc.zeroconf, 3000):
            addresses = info.parsed_addresses()
            if addresses and info.port:
                return addresses[0], info.port
    except Exception as e:  # noqa: BLE001 - discovery is best effort
        _LOGGER.debug("could not resolve %s: %s", device_id, e)
    return None


def device_id_from_name(name: str) -> str | None:
    """"abc123def456._piston._tcp.local." -> "abc123def456" (the device advertises under its id)."""
    suffix = "." + SERVICE_TYPE
    return name[: -len(suffix)].lower() if name.lower().endswith(suffix) else None


async def _default_instance(hass):
    from homeassistant.components import zeroconf

    return await zeroconf.async_get_async_instance(hass)


def _default_browser(zeroconf, types: list, handlers: list):
    from zeroconf.asyncio import AsyncServiceBrowser

    return AsyncServiceBrowser(zeroconf, types, handlers=handlers)


class WakeListener:
    """Calls `client.wake()` when this device announces itself on mDNS, and follows it to a new address.

    An announcement after a wake arrives as `Updated` (the record was still cached), a first one as `Added`; both
    count, `Removed` (the device saying goodbye as it goes down) does not."""

    def __init__(self, hass, device_id: str, client, on_address: Callable[[str, int], None], *,
                 resolve: Callable[..., Awaitable] = resolve_address, get_instance: Callable[..., Awaitable] = _default_instance,
                 browser_factory: Callable = _default_browser):
        self._hass, self._device_id, self._client, self._on_address = hass, device_id.lower(), client, on_address
        self._resolve, self._get_instance, self._browser_factory = resolve, get_instance, browser_factory
        self._browser = None
        self._task: asyncio.Task | None = None

    async def start(self) -> bool:
        """Begin watching; False (and a log line) when mDNS is not available: the probe loop covers for it."""
        try:
            aiozc = await self._get_instance(self._hass)
            self._browser = self._browser_factory(aiozc.zeroconf, [SERVICE_TYPE], [self._on_service])
        except Exception as e:  # noqa: BLE001 - never let a missing helper stop the integration from loading
            _LOGGER.warning("Cannot watch for %s on mDNS (%s); relying on the periodic reconnect", self._device_id, e)
            return False
        return True

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        browser, self._browser = self._browser, None
        if browser is not None:
            try:
                await browser.async_cancel()
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("could not stop the mDNS browser: %s", e)

    def _on_service(self, zeroconf, service_type, name, state_change) -> None:
        if state_change.name == "Removed" or device_id_from_name(name) != self._device_id:
            return
        if self._task is None or self._task.done():   # announcements come in bursts: one follow-up at a time
            self._task = asyncio.get_running_loop().create_task(self._seen())

    async def _seen(self) -> None:
        self._client.wake()                           # first: assume it is where it was
        try:
            found = await self._resolve(self._hass, self._device_id)
        except Exception:  # noqa: BLE001
            found = None
        if found and found != (self._client.host, self._client.port):
            self._client.set_address(*found)
            self._on_address(*found)
            self._client.wake()                       # an attempt to the old address may be running: try the new one next
