import unittest
from unittest import mock

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from . import flowutil
from .stdlib_transport import StdTransport
from custom_components.decky_home_assistant import config_flow as cf
from custom_components.decky_home_assistant.const import DOMAIN
from decky_ha.pairing import PairingManager
from decky_ha.server import ProtocolServer


class DeviceFixture:
    def __init__(self, secret=b""):
        self.box = {"secret": secret}
        self.mgr = PairingManager(lambda: self.box["secret"], lambda s: self.box.update(secret=s))
        self.server = ProtocolServer(device_id=lambda: "dev123abc456", secret=lambda: self.box["secret"],
                                     pairing=self.mgr, commands={}, snapshot=lambda: {}, host="127.0.0.1", port=0,
                                     hello_extra=lambda: {"name": "Test box"})


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dev = DeviceFixture()
        await self.dev.server.start()
        self.addAsyncCleanup(self.dev.server.stop)
        self.hass = HomeAssistant()
        patcher = mock.patch.object(cf, "make_transport", lambda hass: StdTransport())
        patcher.start()
        self.addCleanup(patcher.stop)

    def flow(self):
        f = cf.DeckyConfigFlow()
        f.hass = self.hass
        return f

    def info(self, **props):
        base = {"device_id": "dev123abc456", "name": "Test box", "proto": "1", "paired": "0"}
        return ZeroconfServiceInfo(host="127.0.0.1", port=self.dev.server.port, properties={**base, **props})

    async def discovered(self):
        f = self.flow()
        r = await flowutil.step(f, "zeroconf", self.info())
        self.assertEqual((r["type"], r["step_id"]), ("form", "confirm"))
        self.assertEqual(r["description_placeholders"]["name"], "Test box")
        r = await flowutil.step(f, "confirm", {})
        self.assertEqual(r["step_id"], "pair")
        return f

    async def test_discovered_device_pairs_with_the_right_code(self):
        f = await self.discovered()
        r = await flowutil.step(f, "pair", {"code": self.dev.mgr.code.code})
        self.assertEqual(r["type"], "create_entry")
        self.assertEqual(r["title"], "Test box")
        self.assertEqual(r["data"]["secret"], self.dev.box["secret"].hex())
        self.assertEqual((r["data"]["device_id"], r["data"]["host"], r["data"]["port"]),
                         ("dev123abc456", "127.0.0.1", self.dev.server.port))
        self.assertEqual(r["options"]["turn_off"], "sleep")
        self.assertEqual(r["options"]["pip_position"], "top-right")

    async def test_wrong_code_shows_an_error_and_can_retry(self):
        f = await self.discovered()
        wrong = "000000" if self.dev.mgr.code.code != "000000" else "111111"
        r = await flowutil.step(f, "pair", {"code": wrong})
        self.assertEqual((r["type"], r["errors"]), ("form", {"base": "wrong_code"}))
        r = await flowutil.step(f, "pair", {"code": self.dev.mgr.code.code})
        self.assertEqual(r["type"], "create_entry")

    async def test_locked_device(self):
        f = await self.discovered()
        wrong = "000000" if self.dev.mgr.code.code != "000000" else "111111"
        for _ in range(5):
            await flowutil.step(f, "pair", {"code": wrong})
        r = await flowutil.step(f, "pair", {"code": wrong})
        self.assertEqual(r["errors"], {"base": "code_locked"})

    async def test_same_device_twice_yields_one_entry_and_updates_the_host(self):
        self.hass.config_entries.entries.append(ConfigEntry(
            data={"host": "10.0.0.9", "port": 1}, unique_id="dev123abc456", domain=DOMAIN))
        r = await flowutil.step(self.flow(), "zeroconf", self.info())
        self.assertEqual((r["type"], r["reason"]), ("abort", "already_configured"))
        entry = self.hass.config_entries.entries[0]
        self.assertEqual((entry.data["host"], entry.data["port"]), ("127.0.0.1", self.dev.server.port))

    async def test_device_already_paired_elsewhere_is_refused(self):
        r = await flowutil.step(self.flow(), "zeroconf", self.info(paired="1"))
        self.assertEqual((r["type"], r["reason"]), ("abort", "already_paired"))

    async def test_advert_without_device_id_is_ignored(self):
        r = await flowutil.step(self.flow(), "zeroconf",
                                ZeroconfServiceInfo(host="1.2.3.4", port=1, properties={}))
        self.assertEqual(r["reason"], "not_supported")

    async def test_unreachable_device(self):
        f = self.flow()
        await flowutil.step(f, "zeroconf", ZeroconfServiceInfo(host="127.0.0.1", port=1,
                                                               properties={"device_id": "dev123abc456", "paired": "0"}))
        r = await flowutil.step(f, "pair", {"code": "123456"})
        self.assertEqual(r["errors"], {"base": "cannot_connect"})

    async def test_manual_address_fallback(self):
        f = self.flow()
        r = await flowutil.step(f, "user")
        self.assertEqual(r["step_id"], "user")
        r = await flowutil.step(f, "user", {"host": "127.0.0.1", "port": self.dev.server.port})
        self.assertEqual(r["step_id"], "pair")
        r = await flowutil.step(f, "pair", {"code": self.dev.mgr.code.code})
        self.assertEqual((r["type"], r["title"]), ("create_entry", "Test box"))

    async def test_manual_address_unreachable(self):
        r = await flowutil.step(self.flow(), "user", {"host": "127.0.0.1", "port": 1})
        self.assertEqual(r["errors"], {"base": "cannot_connect"})

    async def test_reauth_replaces_the_secret(self):
        entry = ConfigEntry(data={"device_id": "dev123abc456", "secret": "00" * 32, "host": "127.0.0.1",
                                  "port": self.dev.server.port, "name": "Box"}, entry_id="e1", unique_id="dev123abc456")
        self.hass.config_entries.entries.append(entry)
        f = self.flow()
        f.context["entry_id"] = "e1"
        r = await flowutil.step(f, "reauth", entry.data)
        self.assertEqual(r["step_id"], "reauth_confirm")
        r = await flowutil.step(f, "reauth_confirm", {})
        self.assertEqual(r["step_id"], "pair")
        r = await flowutil.step(f, "pair", {"code": self.dev.mgr.code.code})
        self.assertEqual((r["type"], r["reason"]), ("abort", "reauth_successful"))
        self.assertEqual(entry.data["secret"], self.dev.box["secret"].hex())

    async def test_options_flow_defaults_and_save(self):
        entry = ConfigEntry(options={"turn_off": "shutdown"})
        flow = cf.DeckyConfigFlow.async_get_options_flow(entry)
        flow.config_entry = entry
        form = await flow.async_step_init()
        defaults = {k.key: k.default for k in form["data_schema"].schema}
        self.assertEqual(defaults["turn_off"], "shutdown")
        self.assertEqual((defaults["sensor_interval"], defaults["pip_cameras"], defaults["pip_size"], defaults["pip_duration"]),
                         ("10", [], "medium", "30"))
        saved = await flow.async_step_init({"turn_off": "sleep", "sensor_interval": "60", "pip_duration": "0",
                                            "pip_cameras": ["camera.a"], "pip_position": "left", "pip_size": "large",
                                            "pip_mute": False})
        self.assertEqual(saved["data"]["sensor_interval"], 60)
        self.assertEqual(saved["data"]["pip_duration"], 0)
        self.assertEqual(saved["data"]["pip_cameras"], ["camera.a"])
