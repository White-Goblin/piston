"""Picture-in-picture signalling between the device's player and a camera, with Home Assistant's real helper defaults."""
from unittest import mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.piston import pip as pip_module
from custom_components.piston.const import DOMAIN

from conftest import FakeCamera


@pytest.fixture
async def relay(hass, loaded, device):
    """The entry's PiP relay with a fake camera behind it, and a way to start a session."""
    camera = FakeCamera()
    loaded.pip._get_camera = lambda hass, entity_id: camera
    loaded.pip._parse_candidate = lambda data: data
    loaded.pip._base_url = lambda hass: "http://ha.example:8123"
    with mock.patch.object(pip_module, "CANDIDATE_RETRY_SECONDS", 0):
        yield type("Relay", (), {"camera": camera, "pip": loaded.pip})


async def show(hass, device, camera="camera.door"):
    await hass.services.async_call(DOMAIN, "show_pip", {"entity_id": "select.box_pip_camera", "camera": camera}, blocking=True)
    return device.sent("pip_show")[-1]["session"]


def signals(device):
    return [d for d in device.sent("pip_signal")]


async def test_the_player_starts_negotiates_and_the_answer_and_candidates_go_back(hass, relay, device, wait_until):
    session = await show(hass, device)
    device.event("pip_signal", {"session": session, "kind": "start"})
    await wait_until(lambda: signals(device))
    assert signals(device)[0]["kind"] == "config" and signals(device)[0]["webrtc"] is True
    device.event("pip_signal", {"session": session, "kind": "offer", "sdp": "OFFER"})
    await wait_until(lambda: {s["kind"] for s in signals(device)} >= {"answer", "candidate"})
    assert relay.camera.offers == [("OFFER", session)]
    device.event("pip_signal", {"session": session, "kind": "candidate", "candidate": {"candidate": "c2"}})
    await wait_until(lambda: relay.camera.candidates)
    assert relay.camera.candidates == [(session, {"candidate": "c2"})]


async def test_a_candidate_that_arrives_before_the_camera_knows_the_session_is_retried(hass, relay, device, wait_until):
    session = await show(hass, device)
    relay.camera.candidate_failures = 2
    device.event("pip_signal", {"session": session, "kind": "candidate", "candidate": {"candidate": "late"}})
    await wait_until(lambda: relay.camera.candidates)


async def test_hls_gives_the_player_a_url_it_can_reach(hass, relay, device, wait_until):
    session = await show(hass, device)
    device.event("pip_signal", {"session": session, "kind": "hls"})
    await wait_until(lambda: any(s["kind"] == "hls_url" for s in signals(device)))
    url = next(s["url"] for s in signals(device) if s["kind"] == "hls_url")
    assert url == "http://ha.example:8123/api/hls/tok/master_playlist.m3u8"
    assert relay.camera.stream.providers == ["hls"] and relay.camera.stream.started


async def test_a_camera_without_webrtc_says_so(hass, relay, device, wait_until):
    relay.camera.camera_capabilities = type("C", (), {"frontend_stream_types": {"hls"}})()
    session = await show(hass, device)
    device.event("pip_signal", {"session": session, "kind": "start"})
    await wait_until(lambda: signals(device))
    assert signals(device)[0]["webrtc"] is False


async def test_failures_are_reported_to_the_player_so_it_can_fall_back(hass, relay, device, wait_until):
    session = await show(hass, device)
    relay.camera.offer_error = RuntimeError("DESCRIBE failed: 404")
    device.event("pip_signal", {"session": session, "kind": "offer", "sdp": "OFFER"})
    await wait_until(lambda: any(s["kind"] == "error" for s in signals(device)))
    assert next(s for s in signals(device) if s["kind"] == "error")["code"] == "pip_offer_failed"
    relay.camera.no_stream = True
    device.event("pip_signal", {"session": session, "kind": "hls"})
    await wait_until(lambda: any(s.get("code") == "pip_hls_failed" for s in signals(device)))


async def test_closing_ends_the_camera_session_and_late_messages_are_ignored(hass, relay, device, wait_until):
    session = await show(hass, device)
    device.event("pip_signal", {"session": session, "kind": "close"})
    await wait_until(lambda: relay.camera.closed == [session])
    device.event("pip_signal", {"session": session, "kind": "offer", "sdp": "LATE"})
    device.event("pip_signal", {"session": "unknown", "kind": "start"})
    await hass.async_block_till_done()
    await relay.pip.async_wait_idle()
    assert relay.camera.offers == [] and signals(device) == []


async def test_a_new_overlay_replaces_the_old_session_and_hiding_closes_it(hass, relay, device, wait_until):
    first = await show(hass, device)
    second = await show(hass, device, "camera.drive")
    assert relay.camera.closed == [first]
    await hass.services.async_call(DOMAIN, "hide_pip", {"entity_id": "select.box_pip_camera"}, blocking=True)
    assert relay.camera.closed == [first, second]


async def test_losing_the_device_closes_every_session(hass, relay, device, loaded, wait_until):
    session = await show(hass, device)
    await device.stop()
    await wait_until(lambda: not loaded.connected)
    await wait_until(lambda: relay.camera.closed == [session])


async def test_a_dead_connection_while_signalling_drops_the_session(hass, relay, device, loaded, wait_until):
    session = await show(hass, device)
    device.fail["pip_signal"] = "internal"
    device.event("pip_signal", {"session": session, "kind": "start"})
    await wait_until(lambda: loaded.pip.sessions == {})


async def test_stopping_the_relay_cancels_work_in_flight(hass, relay, loaded):
    loaded.pip.stop()
    assert loaded.pip.sessions == {}


# --- the real helper defaults -------------------------------------------------------------------

async def test_the_default_candidate_parser_builds_the_real_object():
    candidate = pip_module.default_candidate({"candidate": "candidate:1 1 UDP 1 192.168.1.2 5000 typ host", "sdpMid": "0", "sdpMLineIndex": 0})
    assert candidate.candidate.startswith("candidate:1")


async def test_the_default_camera_lookup_refuses_an_unknown_entity(hass):
    with pytest.raises(HomeAssistantError):
        pip_module.default_get_camera(hass, "camera.nowhere")


async def test_the_default_base_url_prefers_the_internal_url(hass):
    await hass.config.async_update(internal_url="http://ha.example:8123/")
    assert pip_module.default_base_url(hass) == "http://ha.example:8123"


async def test_the_default_base_url_falls_back_to_the_local_address(hass):
    from types import SimpleNamespace
    hass.config.api = SimpleNamespace(local_ip="192.168.1.2", host="192.168.1.2", port=8123, use_ssl=False)
    assert pip_module.default_base_url(hass) == "http://192.168.1.2:8123"


async def test_the_default_base_url_explains_what_to_set_when_there_is_nothing_to_offer(hass):
    hass.config.api = None
    with pytest.raises(RuntimeError, match="internal URL"):
        pip_module.default_base_url(hass)
