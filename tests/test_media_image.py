import asyncio
import base64
import os
import unittest

from custom_components.piston.artwork import ArtworkFetcher
from custom_components.piston.client import CommandError, DeviceOffline
from custom_components.piston.media_player import DeckyMediaPlayer
from piston_device import artwork as device_art
from piston_device.proto import messages as m
from .entityutil import connect, make_coordinator


def image(size):
    return b"\xff\xd8\xff" + os.urandom(size)


class Device:
    """The device side of get_artwork (the real ArtStore) behind a command function."""

    def __init__(self, data=None):
        self.store = device_art.ArtStore()
        self.calls = []
        self.offline = False
        self.before_reply = None
        if data:
            self.set(data)

    def set(self, data):
        self.store.set(device_art.make_art(data))
        return self.store.id

    async def command(self, name, data):
        if name != "get_artwork":
            return {}  # for example the set_sensor_interval the coordinator sends on connect
        self.calls.append((name, dict(data)))
        await asyncio.sleep(0)
        if self.before_reply:
            self.before_reply(len(self.calls))
        if self.offline:
            raise DeviceOffline("offline")
        art_id, offset = m.artwork_request(data)
        try:
            content_type, size, piece, done = self.store.chunk(art_id, offset)
        except LookupError:
            raise CommandError(m.NOT_RUNNING, "gone") from None
        return {"content_type": content_type, "size": size, "offset": offset, "data": base64.b64encode(piece).decode(), "done": done}


class FetcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = image(m.ARTWORK_CHUNK_BYTES * 2 + 77)
        self.device = Device(self.data)
        self.current = self.device.store.id
        self.fetcher = ArtworkFetcher(self.device.command, lambda: self.current)

    async def test_the_first_call_fetches_every_chunk_and_later_calls_use_the_cache(self):
        self.assertEqual(await self.fetcher.get(), (self.data, "image/jpeg"))
        self.assertEqual([c[1]["offset"] for c in self.device.calls], [0, m.ARTWORK_CHUNK_BYTES, 2 * m.ARTWORK_CHUNK_BYTES])
        self.assertEqual(await self.fetcher.get(), (self.data, "image/jpeg"))
        self.assertEqual(len(self.device.calls), 3)

    async def test_no_image_id_asks_nothing(self):
        self.current = None
        self.assertEqual(await self.fetcher.get(), (None, None))
        self.assertEqual(self.device.calls, [])

    async def test_simultaneous_calls_share_one_fetch(self):
        results = await asyncio.gather(self.fetcher.get(), self.fetcher.get(), self.fetcher.get())
        self.assertTrue(all(r == (self.data, "image/jpeg") for r in results))
        self.assertEqual(len(self.device.calls), 3)

    async def test_a_new_image_replaces_the_cached_one(self):
        await self.fetcher.get()
        newer = image(1000)
        self.current = self.device.set(newer)
        self.assertEqual(await self.fetcher.get(), (newer, "image/jpeg"))

    async def test_the_track_changing_mid_fetch_restarts_with_the_new_image(self):
        newer = image(5000)

        def swap(call_number):
            if call_number == 2:  # after the first chunk of the old image
                self.current = self.device.set(newer)

        self.device.before_reply = swap
        self.assertEqual(await self.fetcher.get(), (newer, "image/jpeg"))
        self.assertEqual(self.fetcher._cached[0], self.current)
        self.assertNotEqual(self.fetcher._cached[0], device_art.make_art(self.data).id)

    async def test_an_image_that_is_gone_gives_nothing_without_looping(self):
        self.device.store.set(None)
        self.assertEqual(await self.fetcher.get(), (None, None))
        self.assertEqual(len(self.device.calls), 1)

    async def test_an_offline_device_gives_nothing(self):
        self.device.offline = True
        self.assertEqual(await self.fetcher.get(), (None, None))

    async def test_damaged_data_is_rejected_and_not_cached(self):
        real = self.device.command

        async def corrupt(name, data):
            result = await real(name, data)
            if data["offset"] == 0:
                result["data"] = base64.b64encode(b"\xff\xd8\xff" + b"x" * m.ARTWORK_CHUNK_BYTES).decode()
            return result

        fetcher = ArtworkFetcher(corrupt, lambda: self.current)
        self.assertEqual(await fetcher.get(), (None, None))
        self.assertIsNone(fetcher._cached)
        self.assertEqual(await self.fetcher.get(), (self.data, "image/jpeg"))  # a clean fetch still works

    async def test_garbage_replies_give_nothing(self):
        for reply in ({}, {"data": "@@@", "done": True}, {"data": None, "done": True}):
            async def command(name, data, reply=reply):
                return reply
            self.assertEqual(await ArtworkFetcher(command, lambda: self.current).get(), (None, None), msg=repr(reply))

    async def test_a_device_that_never_finishes_is_given_up_on(self):
        async def endless(name, data):
            return {"data": base64.b64encode(b"x").decode(), "done": False, "content_type": "image/jpeg"}
        self.assertEqual(await ArtworkFetcher(endless, lambda: self.current).get(), (None, None))


class EntityTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_media_player_exposes_the_hash_and_fetches_the_bytes(self):
        data = image(3000)
        device = Device(data)
        coord = make_coordinator()
        coord.client.command = device.command
        connect(coord, media={"title": "Ep", "kind": "video", "source": "mpris", "art": device.store.id})
        p = DeckyMediaPlayer(coord)
        self.assertEqual(p.media_image_hash, device.store.id)
        self.assertEqual(await p.async_get_media_image(), (data, "image/jpeg"))

    async def test_no_art_means_no_hash_and_no_image(self):
        coord = make_coordinator()
        connect(coord, media={"title": "Ep"})
        p = DeckyMediaPlayer(coord)
        self.assertIsNone(p.media_image_hash)
        self.assertEqual(await p.async_get_media_image(), (None, None))
        self.assertEqual(coord.client.calls, [])


if __name__ == "__main__":
    unittest.main()
