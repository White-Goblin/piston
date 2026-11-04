"""Now playing details and artwork on the media player, in a real Home Assistant."""
import os

from homeassistant.components.media_player import DATA_COMPONENT

from piston_device.proto.messages import ARTWORK_CHUNK_BYTES

APPS = [{"appid": "250900", "name": "Isaac", "kind": "steam"}, {"appid": "2451505133", "name": "Jellyfin", "kind": "shortcut"}]
RUNNING = {"apps": APPS, "running": True, "appid": "250900", "name": "Isaac", "screen": "game"}


async def settle(hass, wait_until, check):
    await wait_until(check)
    await hass.async_block_till_done()

JPEG = b"\xff\xd8\xff" + os.urandom(ARTWORK_CHUNK_BYTES + 1234)   # more than one chunk


def entity(hass):
    return hass.data[DATA_COMPONENT].get_entity("media_player.box")


async def test_the_details_and_progress_are_shown(hass, loaded, device, state, wait_until):
    device.push("apps", {**RUNNING, "appid": "2451505133", "name": "Jellyfin", "playback": "playing"})
    device.play(JPEG, title="Snowplow Struttin'", artist="Thomas & Friends", album="Season 2", duration=675.1,
                position=12.5, position_at=1_762_000_000.0)
    await settle(hass, wait_until, lambda: hass.states.get("media_player.box").attributes.get("media_title") == "Snowplow Struttin'")
    attributes = hass.states.get("media_player.box").attributes
    assert state("media_player.box") == "playing"
    assert (attributes["media_artist"], attributes["media_album_name"], attributes["media_content_type"]) == (
        "Thomas & Friends", "Season 2", "video")
    assert (attributes["media_duration"], attributes["media_position"]) == (675, 12)
    assert attributes["media_position_updated_at"].timestamp() == 1_762_000_000.0
    assert attributes["media_source"] == "mpris"
    assert "entity_picture" in attributes                       # Home Assistant builds it from media_image_hash


async def test_the_image_is_fetched_in_chunks_and_cached_by_hash(hass, loaded, device, wait_until):
    group = device.play(JPEG)
    await settle(hass, wait_until, lambda: entity(hass).media_image_hash == group["art"])
    assert await entity(hass).async_get_media_image() == (JPEG, "image/jpeg")
    chunks = len(device.sent("get_artwork"))
    assert chunks == 2
    assert await entity(hass).async_get_media_image() == (JPEG, "image/jpeg")
    assert len(device.sent("get_artwork")) == chunks            # served from the cache


async def test_a_new_track_changes_the_hash_and_the_image(hass, loaded, device, wait_until):
    first = device.play(JPEG)
    await settle(hass, wait_until, lambda: entity(hass).media_image_hash == first["art"])
    second_image = b"\x89PNG\r\n\x1a\n" + b"p" * 500
    second = device.play(second_image, title="Ep 2")
    await settle(hass, wait_until, lambda: entity(hass).media_image_hash == second["art"])
    assert await entity(hass).async_get_media_image() == (second_image, "image/png")


async def test_no_image_when_the_device_is_offline_or_has_none(hass, loaded, device, wait_until):
    group = device.play(JPEG)
    await settle(hass, wait_until, lambda: entity(hass).media_image_hash == group["art"])
    device.art.set(None)                                         # the device lost the image
    assert await entity(hass).async_get_media_image() == (None, None)
    device.push("media", {"title": "No art", "kind": "video", "source": "mpris"})
    await settle(hass, wait_until, lambda: entity(hass).media_image_hash is None)
    assert await entity(hass).async_get_media_image() == (None, None)
    assert "entity_picture" not in hass.states.get("media_player.box").attributes


async def test_the_media_is_cleared_when_the_device_sleeps(hass, loaded, device, state, wait_until):
    device.push("apps", RUNNING)
    device.play(JPEG, title="Ep")
    await settle(hass, wait_until, lambda: hass.states.get("media_player.box").attributes.get("media_title") == "Ep")
    device.event("going_to_sleep", {"reason": "suspend"})
    await settle(hass, wait_until, lambda: state("media_player.box") == "standby")
    assert entity(hass).media_image_hash is None
    assert entity(hass).media_artist is None


async def test_the_steam_fallback_shows_an_app(hass, loaded, device, wait_until):
    device.push("apps", {"apps": APPS, "running": True, "appid": "2451505133", "name": "Jellyfin", "screen": "game"})
    group = device.play(JPEG, title="Jellyfin", kind="app", source="steam")
    await settle(hass, wait_until, lambda: hass.states.get("media_player.box").attributes.get("media_content_type") == "app")
    assert await entity(hass).async_get_media_image() == (JPEG, "image/jpeg")
    assert group["source"] == "steam"


async def test_the_image_travels_over_home_assistants_real_websocket_client(hass, real_transport, loaded, device, wait_until):
    """A chunk is about 512 KiB once encoded; aiohttp's default message limit must let it through."""
    group = device.play(JPEG)
    await settle(hass, wait_until, lambda: entity(hass).media_image_hash == group["art"])
    assert await entity(hass).async_get_media_image() == (JPEG, "image/jpeg")
