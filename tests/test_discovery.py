import asyncio
import types
import unittest

from custom_components.decky_home_assistant import discovery
from custom_components.decky_home_assistant.const import SERVICE_TYPE

NAME = "abc123def456." + SERVICE_TYPE


class FakeClient:
    def __init__(self):
        self.host, self.port, self.wakes, self.addresses = "192.168.1.5", 47830, 0, []

    def wake(self):
        self.wakes += 1

    def set_address(self, host, port):
        self.host, self.port = host, port
        self.addresses.append((host, port))


class FakeBrowser:
    def __init__(self):
        self.cancelled = False

    async def async_cancel(self):
        self.cancelled = True


def change(name):
    return types.SimpleNamespace(name=name)


class WakeListenerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client, self.persisted, self.resolves = FakeClient(), [], []
        self.answer = None                       # what the address lookup returns
        self.gate = None                         # an Event the lookup waits for, to hold it mid-way
        self.browsers = []

        async def resolve(hass, device_id):
            self.resolves.append(device_id)
            if self.gate is not None:
                await self.gate.wait()
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        async def get_instance(hass):
            return types.SimpleNamespace(zeroconf="zc")

        def browser_factory(zeroconf, service_types, handlers):
            browser = FakeBrowser()
            self.browsers.append((zeroconf, service_types, handlers, browser))
            return browser

        self.listener = discovery.WakeListener(
            "hass", "ABC123DEF456", self.client, lambda host, port: self.persisted.append((host, port)),
            resolve=resolve, get_instance=get_instance, browser_factory=browser_factory)
        self.addAsyncCleanup(self.listener.stop)

    async def started(self):
        self.assertTrue(await self.listener.start())
        return self.browsers[0][2][0]            # the handler

    async def settle(self):
        for _ in range(10):
            await asyncio.sleep(0)

    async def test_start_watches_the_service_type_with_homes_assistants_shared_instance(self):
        await self.started()
        zeroconf, service_types, handlers, _ = self.browsers[0]
        self.assertEqual((zeroconf, service_types, len(handlers)), ("zc", [SERVICE_TYPE], 1))

    async def test_an_announcement_wakes_the_client_at_once_before_the_lookup_finishes(self):
        handler = await self.started()
        self.gate = asyncio.Event()
        handler(None, SERVICE_TYPE, NAME, change("Updated"))     # what a wake produced on the device
        await self.settle()
        self.assertEqual(self.client.wakes, 1)
        self.assertEqual(self.resolves, ["abc123def456"])
        self.gate.set()
        await self.settle()

    async def test_added_counts_removed_and_other_devices_do_not(self):
        handler = await self.started()
        handler(None, SERVICE_TYPE, NAME, change("Removed"))
        handler(None, SERVICE_TYPE, "ffffffffffff." + SERVICE_TYPE, change("Updated"))
        handler(None, SERVICE_TYPE, "something.else.local.", change("Added"))
        await self.settle()
        self.assertEqual(self.client.wakes, 0)
        handler(None, SERVICE_TYPE, NAME.upper(), change("Added"))   # case does not matter
        await self.settle()
        self.assertEqual(self.client.wakes, 1)

    async def test_a_burst_of_announcements_is_followed_up_once(self):
        handler = await self.started()
        self.gate = asyncio.Event()
        for _ in range(4):
            handler(None, SERVICE_TYPE, NAME, change("Updated"))
        await self.settle()
        self.gate.set()
        await self.settle()
        self.assertEqual(self.resolves, ["abc123def456"])
        self.assertEqual(self.client.wakes, 1)
        handler(None, SERVICE_TYPE, NAME, change("Updated"))     # a later one is handled again
        await self.settle()
        self.assertEqual(self.client.wakes, 2)

    async def test_a_new_address_is_adopted_stored_and_tried_at_once(self):
        handler = await self.started()
        self.answer = ("192.168.1.99", 47831)
        handler(None, SERVICE_TYPE, NAME, change("Updated"))
        await self.settle()
        self.assertEqual(self.client.addresses, [("192.168.1.99", 47831)])
        self.assertEqual(self.persisted, [("192.168.1.99", 47831)])
        self.assertEqual(self.client.wakes, 2)                   # again, in case an attempt to the old address was running

    async def test_the_same_address_changes_nothing(self):
        handler = await self.started()
        self.answer = ("192.168.1.5", 47830)
        handler(None, SERVICE_TYPE, NAME, change("Updated"))
        await self.settle()
        self.assertEqual((self.client.addresses, self.persisted, self.client.wakes), ([], [], 1))

    async def test_a_failed_lookup_still_wakes_the_client(self):
        handler = await self.started()
        self.answer = RuntimeError("zeroconf is busy")
        handler(None, SERVICE_TYPE, NAME, change("Updated"))
        await self.settle()
        self.assertEqual((self.client.wakes, self.persisted), (1, []))

    async def test_without_mdns_the_listener_gives_up_quietly(self):
        async def broken(hass):
            raise ImportError("no zeroconf")

        listener = discovery.WakeListener("hass", "x", self.client, lambda *a: None, get_instance=broken)
        with self.assertLogs(discovery._LOGGER, "WARNING"):
            self.assertFalse(await listener.start())
        await listener.stop()                                    # must not raise

    async def test_stop_cancels_the_browser_and_a_running_follow_up(self):
        handler = await self.started()
        self.gate = asyncio.Event()
        handler(None, SERVICE_TYPE, NAME, change("Updated"))
        await self.settle()
        await self.listener.stop()
        self.assertTrue(self.browsers[0][3].cancelled)
        self.gate.set()
        await self.settle()
        self.assertEqual(self.client.addresses, [])


class NameTests(unittest.TestCase):
    def test_device_id_from_name(self):
        self.assertEqual(discovery.device_id_from_name(NAME), "abc123def456")
        self.assertEqual(discovery.device_id_from_name(NAME.upper()), "abc123def456")      # case does not matter
        self.assertIsNone(discovery.device_id_from_name("abc123def456._http._tcp.local."))
        self.assertIsNone(discovery.device_id_from_name(""))


if __name__ == "__main__":
    unittest.main()
