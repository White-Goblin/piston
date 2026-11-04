"""mDNS lookups and the wake listener with Home Assistant's zeroconf, and Wake on LAN on a real UDP socket."""
import asyncio
import socket
from types import SimpleNamespace
from unittest import mock

import pytest
from homeassistant.setup import async_setup_component
from zeroconf import ServiceStateChange

from custom_components.piston import discovery, wol
from custom_components.piston.const import SERVICE_TYPE

from conftest import DEVICE_ID


class Info:
    """AsyncServiceInfo stand-in: answers (or not) with one address."""
    answer = True

    def __init__(self, service_type, name):
        self.name = name

    async def async_request(self, zeroconf, timeout):
        return Info.answer

    def parsed_addresses(self):
        return ["192.168.1.9"]

    port = 47830


@pytest.fixture
def mdns():
    """A browser class that records what is asked of it, and an address lookup that answers from `Info`."""
    browser = mock.MagicMock()
    browser.return_value.async_cancel = mock.AsyncMock()
    with mock.patch("zeroconf.asyncio.AsyncServiceBrowser", browser), mock.patch("zeroconf.asyncio.AsyncServiceInfo", Info):
        yield browser


async def test_the_address_lookup_returns_what_mdns_answers(hass, mdns):
    assert await async_setup_component(hass, "zeroconf", {})
    Info.answer = True
    assert await discovery.resolve_address(hass, DEVICE_ID) == ("192.168.1.9", 47830)
    Info.answer = False
    assert await discovery.resolve_address(hass, DEVICE_ID) is None            # the device does not answer


async def test_the_address_lookup_gives_up_quietly_when_zeroconf_is_unavailable(hass):
    assert await discovery.resolve_address(hass, DEVICE_ID) is None            # zeroconf was never set up in this Home Assistant


async def test_the_wake_listener_uses_homes_assistants_zeroconf_and_wakes_the_client(hass, mdns):
    assert await async_setup_component(hass, "zeroconf", {})
    client = SimpleNamespace(host="127.0.0.1", port=47830, wakes=0, addresses=[])
    client.wake = lambda: setattr(client, "wakes", client.wakes + 1)
    client.set_address = lambda host, port: client.addresses.append((host, port))
    moved = []
    listener = discovery.WakeListener(hass, DEVICE_ID, client, lambda host, port: moved.append((host, port)))
    assert await listener.start()
    (zc, types, kwargs) = mdns.call_args.args[0], mdns.call_args.args[1], mdns.call_args.kwargs
    assert types == [SERVICE_TYPE] and len(kwargs["handlers"]) == 1
    Info.answer = True
    kwargs["handlers"][0](zc, SERVICE_TYPE, f"{DEVICE_ID}.{SERVICE_TYPE}", ServiceStateChange.Updated)   # what a wake produced
    await hass.async_block_till_done()
    for _ in range(50):
        if moved:
            break
        await asyncio.sleep(0.02)
    assert client.wakes == 2 and client.addresses == [("192.168.1.9", 47830)] and moved == [("192.168.1.9", 47830)]
    await listener.stop()
    mdns.return_value.async_cancel.assert_awaited_once()


async def test_stopping_survives_a_browser_that_will_not_cancel(hass, mdns):
    assert await async_setup_component(hass, "zeroconf", {})
    mdns.return_value.async_cancel.side_effect = RuntimeError("already closed")
    listener = discovery.WakeListener(hass, DEVICE_ID, SimpleNamespace(), lambda *a: None)
    assert await listener.start()
    await listener.stop()                                                         # must not raise


async def test_the_entrys_own_listener_watches_the_service_through_homes_assistants_zeroconf(hass, mdns, loaded):
    assert mdns.call_args.args[1] == [SERVICE_TYPE]                       # created through async_get_async_instance
    assert loaded.wake_listener._browser is mdns.return_value


async def test_a_listener_whose_zeroconf_is_unavailable_reports_that_it_could_not_start(hass, caplog):
    with mock.patch("homeassistant.components.zeroconf.async_get_async_instance", mock.AsyncMock(side_effect=KeyError("zeroconf"))):
        listener = discovery.WakeListener(hass, DEVICE_ID, SimpleNamespace(), lambda *a: None)
        assert not await listener.start()
    assert "relying on the periodic reconnect" in caplog.text
    await listener.stop()


async def test_a_new_address_found_by_the_listener_is_stored_in_the_entry(hass, config_entry, loaded):
    # the entry's own listener persists an address change; trigger that path directly
    callback = loaded.wake_listener._on_address
    callback("192.168.1.77", 47831)
    assert (config_entry.data["host"], config_entry.data["port"]) == ("192.168.1.77", 47831)


# --- Wake on LAN ---------------------------------------------------------------------------------

def test_the_magic_packet_is_six_ff_then_the_mac_sixteen_times():
    packet = wol.magic_packet("AA-BB-CC-DD-EE-FF")
    assert packet == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16
    with pytest.raises(ValueError):
        wol.magic_packet("not a mac")


async def test_the_packet_goes_out_on_both_ports_over_real_udp(hass, socket_enabled):
    receivers = []
    ports = []
    for _ in range(2):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(2)
        receivers.append(sock)
        ports.append(sock.getsockname()[1])
    with mock.patch.object(wol, "PORTS", tuple(ports)):
        await wol.send_magic_packet("aa:bb:cc:dd:ee:ff", "127.0.0.1")
    for sock in receivers:
        assert sock.recvfrom(200)[0] == wol.magic_packet("aa:bb:cc:dd:ee:ff")
        sock.close()
