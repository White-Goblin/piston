"""Helpers for entity tests: a coordinator with a recording fake client."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.piston.client import CommandError, DeviceOffline
from custom_components.piston.coordinator import DeckyCoordinator


class FakeClient:
    def __init__(self):
        self.calls = []
        self.error = None
        self.connected = True

    async def command(self, name, data=None):
        self.calls.append((name, data))
        if not self.connected:
            raise DeviceOffline("offline")
        if self.error:
            raise self.error
        return {}


def make_coordinator(options=None, data=None):
    entry = ConfigEntry(data={"device_id": "dev123abc456", "secret": "00", "host": "h", "port": 1, "name": "Box",
                              **(data or {})}, options=options or {}, unique_id="dev123abc456")
    coord = DeckyCoordinator(HomeAssistant(), entry, off_timeout=3600)
    coord.client = FakeClient()
    return coord


def connect(coord, **groups):
    coord.on_connection(True)
    for name, data in groups.items():
        coord.on_state(name, data)
