import asyncio
import json
import time
import unittest

import tests
from . import use_temp_settings_dir
from piston_device import cursor
from piston_device.cursor import CursorFeed
from piston_device.http_server import HttpServerMixin
from piston_device.input import InputMixin
from piston_device.settings import SettingsMixin
from piston_device.state import StateMixin


def pos(x=1, y=2, d=False):
    return {"x": x, "y": y, "s": 0.0, "d": d}


class FeedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tests.fake.emitted.clear()
        self.now = 0.0
        self.feed = CursorFeed(clock=lambda: self.now)

    async def test_positions_are_numbered_and_a_poll_returns_everything_since_after(self):
        for i in range(3):
            self.feed.publish(pos(i))
        first = await self.feed.poll(0)
        self.assertEqual((first["seq"], [i["x"] for i in first["items"]]), (3, [0, 1, 2]))
        self.feed.publish(pos(9))
        second = await self.feed.poll(3)
        self.assertEqual([i["x"] for i in second["items"]], [9])
        self.assertEqual([i["n"] for i in second["items"]], [4])

    async def test_a_poll_with_nothing_new_waits_and_is_woken_by_the_next_position(self):
        task = asyncio.create_task(self.feed.poll(0, timeout=5))
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        self.feed.publish(pos(7))
        result = await asyncio.wait_for(task, 1)
        self.assertEqual([i["x"] for i in result["items"]], [7])

    async def test_a_quiet_poll_ends_empty_after_the_timeout(self):
        started = time.monotonic()
        result = await self.feed.poll(0, timeout=0.05)
        self.assertEqual((result["items"], result["seq"]), ([], 0))
        self.assertGreaterEqual(time.monotonic() - started, 0.04)

    async def test_only_the_last_120_positions_are_kept(self):
        for i in range(300):
            self.feed.publish(pos(i))
        result = await self.feed.poll(0)
        self.assertEqual(len(result["items"]), cursor.KEEP)
        self.assertEqual(result["items"][-1]["x"], 299)

    async def test_a_page_ahead_of_the_feed_is_told_to_start_over(self):
        self.feed.publish(pos())
        result = await self.feed.poll(50)
        self.assertEqual((result["reset"], result["seq"], result["items"]), (True, 1, []))

    async def test_stop_is_an_item_the_page_can_see(self):
        self.feed.publish(pos())
        self.feed.stop()
        result = await self.feed.poll(0)
        self.assertEqual(result["items"][-1].get("stop"), True)

    async def test_stop_wakes_a_waiting_poll(self):
        task = asyncio.create_task(self.feed.poll(0, timeout=5))
        await asyncio.sleep(0.01)
        self.feed.stop()
        self.assertTrue((await asyncio.wait_for(task, 1))["items"][0]["stop"])

    async def test_abort_answers_every_waiting_poll(self):
        tasks = [asyncio.create_task(self.feed.poll(0, timeout=30)) for _ in range(3)]
        await asyncio.sleep(0.01)
        self.feed.abort()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 1)
        self.assertTrue(all(r["items"] == [] for r in results))

    async def test_the_activity_event_is_sent_on_the_first_position_and_then_only_when_it_matters(self):
        self.feed.publish(pos())                                       # the view must be created
        self.feed.publish(pos())
        self.feed.publish(pos())
        await asyncio.sleep(0)
        self.assertEqual(tests.fake.emitted, [("cursor_activity", ({"down": False},))])
        self.now = 0.5                                                  # past the keep-alive interval
        self.feed.publish(pos())
        self.feed.publish(pos(d=True))                                  # a click changes the state: sent at once
        await asyncio.sleep(0)
        self.assertEqual([a[0]["down"] for e, a in tests.fake.emitted], [False, False, True])

    async def test_hide_and_show_go_to_the_frontend(self):
        await self.feed.hide(True)
        await self.feed.hide(False)
        self.assertEqual(tests.fake.emitted, [("cursor_view", ({"visible": False},)), ("cursor_view", ({"visible": True},))])

    def test_publishing_without_a_running_loop_does_not_raise(self):
        CursorFeed().publish(pos())


class Host(SettingsMixin, StateMixin, InputMixin, HttpServerMixin):
    def __init__(self):
        self._init_state()
        self._init_input()
        self.nonce = "secret-nonce"
        self.http_port = 4242


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        use_temp_settings_dir()
        tests.fake.emitted.clear()
        self.h = Host()
        self.addCleanup(self.h.input_keys.shutdown, False)

    def request(self, path, method="GET"):
        return self.h._handle_request(method, path)

    async def test_a_good_poll_returns_json_with_cors_for_the_inline_page(self):
        self.h.cursor_feed.publish(pos(5))
        response = await self.request("/cursor/events?nonce=secret-nonce&after=0")
        head, _, body = response.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        self.assertIn(b"Access-Control-Allow-Origin: *", head)
        self.assertIn(b"Cache-Control: no-store", head)
        self.assertIn(b"Content-Type: application/json", head)
        self.assertIn(f"Content-Length: {len(body)}".encode(), head)
        data = json.loads(body)
        self.assertEqual((data["seq"], data["items"][0]["x"]), (1, 5))

    async def test_the_poll_waits_for_news(self):
        task = asyncio.create_task(self.request("/cursor/events?nonce=secret-nonce&after=0"))
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        self.h.cursor_feed.publish(pos(8))
        body = (await asyncio.wait_for(task, 1)).partition(b"\r\n\r\n")[2]
        self.assertEqual(json.loads(body)["items"][0]["x"], 8)

    async def test_wrong_or_missing_nonce_is_refused(self):
        self.assertTrue((await self.request("/cursor/events?nonce=nope&after=0")).startswith(b"HTTP/1.1 403"))
        self.assertTrue((await self.request("/cursor/events?after=0")).startswith(b"HTTP/1.1 403"))

    async def test_only_get_is_allowed_and_a_bad_after_is_a_bad_request(self):
        self.assertTrue((await self.request("/cursor/events?nonce=secret-nonce", "POST")).startswith(b"HTTP/1.1 405"))
        self.assertTrue((await self.request("/cursor/events?nonce=secret-nonce&after=x")).startswith(b"HTTP/1.1 400"))

    async def test_a_negative_after_is_treated_as_zero(self):
        self.h.cursor_feed.publish(pos(1))
        body = (await self.request("/cursor/events?nonce=secret-nonce&after=-5")).partition(b"\r\n\r\n")[2]
        self.assertEqual(len(json.loads(body)["items"]), 1)

    async def test_no_feed_is_not_found(self):
        self.h.cursor_feed = None
        self.assertTrue((await self.request("/cursor/events?nonce=secret-nonce&after=0")).startswith(b"HTTP/1.1 404"))

    async def test_other_routes_have_no_cors_header(self):
        response = await self.request("/nothing")
        self.assertNotIn(b"Access-Control-Allow-Origin", response)

    async def test_the_cursor_config_callable_gives_port_and_nonce(self):
        self.assertEqual(await self.h.get_cursor_config(), {"port": 4242, "nonce": "secret-nonce"})

    async def test_unload_cleanup_answers_waiting_polls(self):
        task = asyncio.create_task(self.request("/cursor/events?nonce=secret-nonce&after=0"))
        await asyncio.sleep(0.02)
        self.h._input_abort()
        response = await asyncio.wait_for(task, 1)
        self.assertTrue(response.startswith(b"HTTP/1.1 200"))
