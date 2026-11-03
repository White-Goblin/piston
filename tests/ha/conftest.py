"""Fixtures for running the integration in a real Home Assistant (see README.md)."""
import asyncio

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from unittest import mock  # noqa: E402

from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.decky_home_assistant import config_flow  # noqa: E402
from custom_components.decky_home_assistant.const import DOMAIN  # noqa: E402
from decky_ha.pairing import PairingManager  # noqa: E402
from decky_ha.proto.messages import ProtocolError  # noqa: E402
from decky_ha.server import ProtocolServer  # noqa: E402
from tests.stdlib_transport import StdTransport  # noqa: E402

SECRET = b"s" * 32
DEVICE_ID = "dev123abc456"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/ from this repository."""
    yield


COMMANDS = ("launch", "quit", "power", "audio", "media_key", "toast", "pip_show", "pip_hide", "pip_signal", "set_sensor_interval")


class FakeDevice:
    """The real device-side protocol server on loopback, with a pairing manager. Every command is recorded in `commands`;
    `fail` maps a command name to an error code the device should answer with instead."""

    def __init__(self, secret=SECRET):
        self.box = {"secret": secret}
        self.pairing = PairingManager(lambda: self.box["secret"], lambda s: self.box.update(secret=s))
        self.commands = []
        self.fail = {}
        self.delay = {}          # command name -> seconds the device takes before answering
        self.groups = {"device": {"device_id": DEVICE_ID, "name": "Box", "mac": "aa:bb:cc:dd:ee:ff", "broadcast": "10.0.0.255",
                                  "protocol": 2, "session": "game"}}

        def handler(name):
            async def run(data):
                self.commands.append((name, data))
                if self.delay.get(name):
                    await asyncio.sleep(self.delay[name])
                if name in self.fail:
                    raise ProtocolError(self.fail[name], f"{name} failed on purpose")
                return {}
            return run

        async def unpair(data):
            self.commands.append(("unpair", data))
            self.pairing.unpair()
            return {}

        self.server = ProtocolServer(
            device_id=lambda: DEVICE_ID, secret=lambda: self.box["secret"], pairing=self.pairing,
            commands={**{name: handler(name) for name in COMMANDS}, "unpair": unpair}, snapshot=lambda: dict(self.groups),
            hello_extra=lambda: {"name": "Box"}, host="127.0.0.1", port=0, ping_interval=5, idle_timeout=30)

    async def start(self):
        await self.server.start()
        return self

    @property
    def port(self) -> int:
        return self.server.port

    def push(self, name, data):
        """The device reports a new value for a state group."""
        self.groups[name] = data
        self.server.push_state(name, data)

    def event(self, name, data=None):
        self.server.push_event(name, data)

    def sent(self, name):
        """The data of every `name` command received so far."""
        return [data for command, data in self.commands if command == name]

    async def stop(self):
        await self.server.stop()


@pytest.fixture
async def device(socket_enabled):
    """The fake device needs a loopback socket, which Home Assistant's test plugin blocks unless asked."""
    fake = await FakeDevice().start()
    yield fake
    await fake.stop()


@pytest.fixture(autouse=True)
def loopback_transport(request):
    """The integration's websocket goes through the stdlib test transport instead of Home Assistant's aiohttp session,
    unless the test asks for the real one with the `real_transport` fixture."""
    if "real_transport" in request.fixturenames:
        yield
        return
    with mock.patch.object(config_flow, "make_transport", lambda hass: StdTransport()):
        yield


@pytest.fixture(autouse=True)
async def zeroconf_mocks(hass, mock_async_zeroconf):
    """Home Assistant loads its zeroconf component as a dependency of this integration. Use its test mocks (no real multicast
    in tests) and stop it here, while they are still in place: its own teardown would run after they are removed."""
    yield mock_async_zeroconf
    hass.bus.async_fire("homeassistant_stop")
    await hass.async_block_till_done()


@pytest.fixture
def real_transport():
    """Marker fixture: use Home Assistant's real aiohttp websocket client (see loopback_transport)."""
    return True


@pytest.fixture
def wait_until():
    """await wait_until(lambda: condition): poll briefly, fail if the condition never becomes true."""
    import asyncio

    async def wait(predicate, tries=200):
        for _ in range(tries):
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("condition not reached")

    return wait


@pytest.fixture
def config_entry(device):
    return MockConfigEntry(
        domain=DOMAIN, unique_id=DEVICE_ID, title="Box", options={"sensor_interval": 60},
        data={"device_id": DEVICE_ID, "secret": SECRET.hex(), "host": "127.0.0.1", "port": device.port, "name": "Box"})


@pytest.fixture
async def loaded(hass, config_entry, device, wait_until):
    """The entry set up and connected to the fake device; unloaded again afterwards. Returns the coordinator."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    coordinator = config_entry.runtime_data
    await wait_until(lambda: coordinator.connected)
    await hass.async_block_till_done()
    yield coordinator
    if hass.config_entries.async_get_entry(config_entry.entry_id) is not None:     # a test may have removed it
        await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def state(hass):
    """state("media_player.box") -> the state string, or None while the entity does not exist yet."""
    return lambda entity_id: getattr(hass.states.get(entity_id), "state", None)


@pytest.fixture
async def unpaired_device(socket_enabled):
    """A device with no pairing yet: its pairing code is `unpaired_device.pairing.code.code`."""
    fake = await FakeDevice(secret=b"").start()
    yield fake
    await fake.stop()


class FakeMessage:
    def __init__(self, body):
        self.body = body

    def as_dict(self):
        return self.body


class FakeStream:
    def __init__(self):
        self.providers, self.started = [], False

    def add_provider(self, fmt):
        self.providers.append(fmt)

    async def start(self):
        self.started = True

    def endpoint_url(self, fmt):
        return "/api/hls/tok/master_playlist.m3u8"


class FakeCamera:
    """The part of a camera entity the PiP relay talks to (WebRTC handlers and an HLS stream)."""

    def __init__(self, webrtc=True, offer_error=None, no_stream=False):
        self.camera_capabilities = type("C", (), {"frontend_stream_types": {"hls", "web_rtc"} if webrtc else {"hls"}})()
        self.offers, self.candidates, self.closed = [], [], []
        self.offer_error, self.no_stream, self.stream = offer_error, no_stream, FakeStream()
        self.candidate_failures = 0

    def async_get_webrtc_client_configuration(self):
        return type("Cfg", (), {"to_frontend_dict": lambda s: {"configuration": {"iceServers": [{"urls": ["stun:x"]}]}}})()

    async def async_handle_async_webrtc_offer(self, sdp, session, send):
        self.offers.append((sdp, session))
        if self.offer_error:
            raise self.offer_error
        send(FakeMessage({"type": "session", "session_id": session}))
        send(FakeMessage({"type": "answer", "answer": "ANSWER-SDP"}))
        send(FakeMessage({"type": "candidate", "candidate": {"candidate": "c1"}}))

    async def async_on_webrtc_candidate(self, session, candidate):
        if self.candidate_failures:
            self.candidate_failures -= 1
            raise RuntimeError("unknown session")
        self.candidates.append((session, candidate))

    def close_webrtc_session(self, session):
        self.closed.append(session)

    async def async_create_stream(self):
        return None if self.no_stream else self.stream
