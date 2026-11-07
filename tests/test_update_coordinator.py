import unittest

from homeassistant.core import HomeAssistant

from custom_components.piston import update_logic as ul
from custom_components.piston.client import DeviceOffline
from custom_components.piston.coordinator import DeckyCoordinator

from .test_coordinator import entry

SHA = "c" * 64


class FakeClient:
    def __init__(self):
        self.sent = []
        self.offline = False

    async def send_update(self, version, url, sha256):
        if self.offline:
            raise DeviceOffline("offline")
        self.sent.append((version, url, sha256))


class UpdateCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.texts = {}
        self.fetches = []

        async def fetch_text(url):
            self.fetches.append(url)
            if url not in self.texts:
                raise OSError("404")
            return self.texts[url]

        self.coord = DeckyCoordinator(HomeAssistant(), entry(), off_timeout=60, target_version="0.4.1", fetch_text=fetch_text)
        self.addCleanup(self.coord.shutdown)
        self.coord.client = FakeClient()
        self.texts[ul.asset_url("0.4.1", "SHA256SUMS")] = f"{SHA}  {ul.zip_name('0.4.1')}\n"

    async def settle(self):
        if self.coord._decide_task is not None:
            await self.coord._decide_task

    def hello(self, version="0.4.0", skew=None, **kw):
        self.coord.on_hello({"type": "hello", "plugin_version": version, "lane": 1, **kw}, skew)

    async def test_an_older_plugin_with_auto_on_gets_one_update(self):
        self.hello("0.4.0")
        await self.settle()
        self.assertEqual(self.coord.client.sent, [("0.4.1", ul.asset_url("0.4.1"), SHA)])
        self.assertEqual(self.coord.update_state, "available")

    async def test_auto_off_only_marks_it_available_and_install_sends(self):
        self.coord.set_auto_update(False)
        self.hello("0.4.0")
        await self.settle()
        self.assertEqual(self.coord.client.sent, [])
        self.assertEqual(self.coord.update_state, "available")
        await self.coord.async_install_update()
        self.assertEqual(len(self.coord.client.sent), 1)

    async def test_turning_auto_on_checks_again(self):
        self.coord.set_auto_update(False)
        self.hello("0.4.0")
        await self.settle()
        self.coord.set_auto_update(True)
        await self.settle()
        self.assertEqual(len(self.coord.client.sent), 1)

    async def test_an_up_to_date_plugin_sends_nothing(self):
        self.hello("0.4.1")
        await self.settle()
        self.assertEqual((self.coord.client.sent, self.coord.update_state), ([], "up_to_date"))

    async def test_a_lane_less_device_is_left_alone(self):
        self.coord.on_hello({"type": "hello"}, None)
        await self.settle()
        self.assertEqual((self.coord.client.sent, self.coord.update_state), ([], "no_lane"))

    async def test_a_target_before_the_lane_is_not_sent(self):
        self.coord.target_version = "0.3.0"
        self.hello("0.4.0")
        await self.settle()
        self.assertEqual((self.coord.client.sent, self.coord.update_state), ([], "predates_lane"))

    async def test_a_missing_checksum_is_cannot_determine_and_sends_nothing(self):
        self.texts.clear()
        self.hello("0.4.0")
        await self.settle()
        self.assertEqual((self.coord.client.sent, self.coord.update_state), ([], "cannot_determine"))

    async def test_a_failure_latches_and_blocks_a_retry_until_install_or_a_new_target(self):
        self.hello("0.4.0")
        await self.settle()
        self.coord.on_update_status({"type": "update_status", "state": "failed", "version": "0.4.1", "reason": "hash"})
        self.assertEqual((self.coord.update_latch, self.coord.update_state), ({"version": "0.4.1", "reason": "hash"}, "failed"))
        self.hello("0.4.0")                                  # reconnect: no retry
        await self.settle()
        self.assertEqual(len(self.coord.client.sent), 1)
        await self.coord.async_install_update()              # a press retries
        self.assertEqual(len(self.coord.client.sent), 2)
        self.coord.on_update_status({"type": "update_status", "state": "failed", "version": "0.4.1", "reason": "hash"})
        self.coord.target_version = "0.4.2"                  # a new target is tried
        self.texts[ul.asset_url("0.4.2", "SHA256SUMS")] = f"{SHA}  {ul.zip_name('0.4.2')}\n"
        self.hello("0.4.0")
        await self.settle()
        self.assertEqual(len(self.coord.client.sent), 3)

    async def test_arriving_at_the_target_clears_everything(self):
        self.coord.update_latch = {"version": "0.4.1", "reason": "hash"}
        self.coord.update_status = {"state": "installing", "version": "0.4.1", "reason": None}
        self.hello("0.4.1")
        self.assertEqual((self.coord.update_latch, self.coord.update_status), (None, None))

    async def test_an_install_that_comes_back_on_another_version_is_a_version_failure(self):
        self.hello("0.4.0")
        await self.settle()
        self.coord.on_update_status({"type": "update_status", "state": "installing", "version": "0.4.1"})
        self.assertTrue(self.coord.update_in_progress)
        self.hello("0.4.0")                                  # back, but still the old version
        await self.settle()
        self.assertEqual(self.coord.update_latch, {"version": "0.4.1", "reason": "version"})

    async def test_an_update_in_progress_is_not_sent_again(self):
        self.coord.on_update_status({"type": "update_status", "state": "downloading", "version": "0.4.1"})
        self.hello("0.4.0")
        await self.settle()
        self.assertEqual(self.coord.client.sent, [])
        self.assertEqual(self.coord.update_state, "in_progress")

    async def test_a_device_that_went_offline_while_sending_is_not_an_error(self):
        self.coord.client.offline = True
        self.hello("0.4.0")
        await self.settle()
        self.assertEqual(self.coord.client.sent, [])

    async def test_the_lane_connection_also_decides(self):
        self.hello("0.4.0", skew="old")
        await self.settle()
        self.assertEqual((self.coord.skewed, len(self.coord.client.sent)), ("old", 1))

    async def test_facts_round_trip_through_the_store_shape(self):
        self.coord.set_auto_update(False)
        self.coord.update_latch = {"version": "0.4.1", "reason": "hash"}
        self.hello("0.4.0")
        facts = self.coord.update_facts()
        self.assertEqual(facts, {"auto_update": False, "plugin_version": "0.4.0", "latch": {"version": "0.4.1", "reason": "hash"}})
        other = DeckyCoordinator(HomeAssistant(), entry(), off_timeout=60, target_version="0.4.1")
        self.addCleanup(other.shutdown)
        other.restore_update(facts)
        self.assertEqual((other.auto_update, other.plugin_version, other.update_latch), (False, "0.4.0", {"version": "0.4.1", "reason": "hash"}))

    async def test_no_target_version_disables_updates(self):
        self.coord.target_version = None
        self.hello("0.4.0")
        await self.settle()
        self.assertEqual(self.coord.client.sent, [])
