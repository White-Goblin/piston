"""Find a known device again after its address changed (backup to Home Assistant's own discovery)."""
from __future__ import annotations

import logging

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
