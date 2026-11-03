"""Branches that only a particular situation reaches: flow edge cases, odd device states, error paths of the real transport."""
import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.decky_home_assistant import coordinator as coordinator_module, discovery, pip as pip_module, store as store_module
from custom_components.decky_home_assistant.client import ConnectionClosed, DeviceOffline
from custom_components.decky_home_assistant.const import DOMAIN
from custom_components.decky_home_assistant.diagnostics import async_get_config_entry_diagnostics
from custom_components.decky_home_assistant.media_player import DeckyMediaPlayer
from custom_components.decky_home_assistant.select import PipCameraSelect
from custom_components.decky_home_assistant.transport import AiohttpTransport

from conftest import DEVICE_ID
from test_config_flow import announcement, discover

from conftest import FakeCamera


# --- config flow edge cases while pairing -----------------------------------------------------------

async def to_pair_step(hass, device):
    result = await discover(hass, device)
    return await hass.config_entries.flow.async_configure(result["flow_id"], {})


async def test_the_device_vanishing_during_pairing_shows_cannot_connect(hass, unpaired_device):
    result = await to_pair_step(hass, unpaired_device)
    await unpaired_device.stop()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["errors"] == {"base": "cannot_connect"}
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_a_device_that_got_paired_by_someone_else_meanwhile_is_refused(hass, unpaired_device):
    result = await to_pair_step(hass, unpaired_device)
    unpaired_device.box["secret"] = b"x" * 32
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_paired")


async def test_a_different_device_answering_at_the_address_aborts_the_pairing(hass, unpaired_device):
    result = await to_pair_step(hass, unpaired_device)
    unpaired_device.server._device_id = lambda: "someone-else"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": unpaired_device.pairing.code.code})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "wrong_device")


# --- coordinator ---------------------------------------------------------------------------------------

async def test_a_silent_loss_becomes_off_after_the_timeout(hass, loaded, device, wait_until):
    with mock.patch.object(coordinator_module, "OFF_TIMEOUT", 0.05):
        loaded._off_timeout = 0.05
        await device.stop()                                          # the connection ends with no sleep or shutdown announcement
        await wait_until(lambda: hass.states.get("media_player.box").state == "off")
    assert hass.states.get("media_player.box").state == "off"


async def test_commands_without_a_client_say_the_device_is_offline(loaded):
    client, loaded.client = loaded.client, None
    with pytest.raises(DeviceOffline):
        await loaded.async_command("power", {"action": "restart"})
    loaded.client = client


# --- diagnostics -----------------------------------------------------------------------------------------

async def test_diagnostics_number_the_connected_controllers_and_keep_the_seen_list_readable(hass, config_entry, loaded, device, wait_until):
    pad = {"name": "Pro 2", "battery": 52, "charging": False, "wireless": True, "stale": False, "updated": "t"}
    device.push("controllers", {"connected": {"AB12CD34": pad}, "seen": [{"serial": "AB12CD34", "name": "Pro 2"}]})
    await wait_until(lambda: "controllers" in loaded.groups and "controllers" not in loaded.restored)
    report = await async_get_config_entry_diagnostics(hass, config_entry)
    assert list(report["groups"]["controllers"]["connected"]) == ["controller_1"]
    assert report["groups"]["controllers"]["seen"] == [{"serial": "**REDACTED**", "name": "Pro 2"}]
    assert "AB12CD34" not in str(report)


# --- discovery -----------------------------------------------------------------------------------------------

async def test_the_wake_listener_ignores_goodbyes_and_other_devices_and_survives_a_failed_lookup(hass):
    client = SimpleNamespace(host="127.0.0.1", port=1, wakes=0)
    client.wake = lambda: setattr(client, "wakes", client.wakes + 1)
    lookup = mock.AsyncMock(side_effect=RuntimeError("zeroconf is busy"))
    listener = discovery.WakeListener(hass, DEVICE_ID, client, lambda *a: None, resolve=lookup)
    from zeroconf import ServiceStateChange
    listener._on_service(None, "t", f"{DEVICE_ID}._deckyha._tcp.local.", ServiceStateChange.Removed)
    listener._on_service(None, "t", "other._deckyha._tcp.local.", ServiceStateChange.Added)
    await asyncio.sleep(0)
    assert client.wakes == 0
    listener._on_service(None, "t", f"{DEVICE_ID}._deckyha._tcp.local.", ServiceStateChange.Added)
    await listener._task
    assert client.wakes == 1                                             # woken even though the lookup failed


# --- media player ----------------------------------------------------------------------------------------------

async def test_media_player_edge_states(hass, loaded, device, wait_until):
    player = DeckyMediaPlayer(loaded)
    loaded.on_state("apps", {"apps": [{"appid": "1", "name": "A"}], "running": True, "appid": "99", "name": "Mystery", "screen": "game"})
    assert player.source is None                                         # something started outside the offered list
    loaded.on_state("apps", {"apps": [], "running": False, "screen": "desktop"})
    assert player.source is None                                         # desktop mode has no source
    loaded.groups["device"] = {k: v for k, v in loaded.groups["device"].items() if k != "mac"}
    hass.config_entries.async_update_entry(loaded.entry, data={k: v for k, v in loaded.entry.data.items() if k != "mac"})
    loaded.on_event("going_to_sleep", {})
    with pytest.raises(ServiceValidationError, match="MAC address"):
        await player.async_turn_on()


async def test_media_player_with_the_apps_still_from_the_last_run(hass, hass_storage, config_entry, device):
    hass_storage[f"decky_home_assistant.{config_entry.entry_id}"] = {
        "version": 1, "minor_version": 1, "key": f"decky_home_assistant.{config_entry.entry_id}",
        "data": {"groups": {"apps": {"apps": [{"appid": "1", "name": "A"}]}}, "last_connected": None}}
    await device.stop()
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert DeckyMediaPlayer(config_entry.runtime_data).source is None    # restored apps are only a list, not what is running
    assert MediaPlayerEntityFeature.SELECT_SOURCE in DeckyMediaPlayer(config_entry.runtime_data).supported_features
    await hass.config_entries.async_unload(config_entry.entry_id)


# --- select and pip ---------------------------------------------------------------------------------------------------

async def test_select_refuses_an_unknown_camera_directly_and_reports_a_failed_hide(hass, config_entry, loaded, device):
    hass.config_entries.async_update_entry(config_entry, options={**config_entry.options, "pip_cameras": ["camera.door"]})
    await hass.async_block_till_done()
    select = PipCameraSelect(loaded)
    with pytest.raises(ServiceValidationError):
        await select.async_select_option("camera.nowhere")               # the service layer normally stops this earlier
    device.fail["pip_hide"] = "internal"
    with pytest.raises(HomeAssistantError):
        await select.async_hide_pip()
    await hass.services.async_call(DOMAIN, "show_pip", {"entity_id": "select.box_pip_camera", "camera": "camera.door"}, blocking=True)


async def test_the_relay_gives_up_on_a_candidate_the_camera_never_accepts(hass, loaded, device, wait_until):
    camera = FakeCamera()
    camera.candidate_failures = 99
    loaded.pip._get_camera = lambda hass, entity_id: camera
    loaded.pip._parse_candidate = lambda data: data
    with mock.patch.object(pip_module, "CANDIDATE_RETRIES", 3), mock.patch.object(pip_module, "CANDIDATE_RETRY_SECONDS", 0):
        await hass.services.async_call(DOMAIN, "show_pip", {"entity_id": "select.box_pip_camera", "camera": "camera.door"}, blocking=True)
        session = device.sent("pip_show")[-1]["session"]
        device.event("pip_signal", {"session": session, "kind": "candidate", "candidate": {"candidate": "x"}})
        await wait_until(lambda: any(s.get("code") == "pip_candidate_failed" for s in device.sent("pip_signal")))


async def test_closing_an_unknown_session_and_waiting_for_signalling_work(hass, loaded, device):
    loaded.pip._close("never-existed")                                   # nothing to do, nothing raised
    camera = FakeCamera()
    loaded.pip._get_camera = lambda hass, entity_id: camera
    await hass.services.async_call(DOMAIN, "show_pip", {"entity_id": "select.box_pip_camera", "camera": "camera.door"}, blocking=True)
    session = device.sent("pip_show")[-1]["session"]
    device.event("pip_signal", {"session": session, "kind": "start"})
    await asyncio.sleep(0.05)
    await loaded.pip.async_wait_idle()
    loaded.pip._tasks.add(asyncio.get_running_loop().create_task(asyncio.sleep(30)))
    loaded.pip.stop()                                                    # cancels what is still in flight
    await asyncio.sleep(0)


# --- stored state --------------------------------------------------------------------------------------------------------------

async def test_the_storage_format_migrates_old_versions_to_nothing_and_refuses_newer_ones(hass):
    storage = store_module._Storage(hass, 1, "decky_home_assistant.test")
    assert await storage._async_migrate_func(0, 1, {"anything": 1}) == {}
    with pytest.raises(NotImplementedError):
        await storage._async_migrate_func(2, 1, {})


async def test_stored_state_with_an_unexpected_shape_is_ignored_with_one_warning(hass, hass_storage, config_entry, device, caplog):
    key = f"decky_home_assistant.{config_entry.entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"groups": ["not", "a", "mapping"]}}
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert caplog.text.count("unexpected shape") == 1
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_the_hourly_check_finds_a_connected_device_with_an_issue_still_open(hass, loaded):
    monitor = loaded.unreachable
    monitor._open = True                                                 # as if the connection notification had been missed
    monitor.check()
    assert monitor._open is False


# --- the real aiohttp transport -----------------------------------------------------------------------------------------------

async def test_the_real_transport_reports_an_unreachable_host_as_an_oserror(hass, socket_enabled):
    with pytest.raises(OSError):
        await AiohttpTransport(async_get_clientsession(hass)).connect("127.0.0.1", 1)


async def test_the_real_transport_reports_the_device_closing_the_connection(hass, device, socket_enabled):
    conn = await AiohttpTransport(async_get_clientsession(hass)).connect("127.0.0.1", device.port)
    await conn.recv(2)                                                   # the device's hello
    await conn.send("this is not json")                                  # the device answers with an error and closes
    try:
        with pytest.raises(ConnectionClosed):
            for _ in range(5):
                await conn.recv(2)
        with pytest.raises(ConnectionClosed):                            # writing to the closed socket
            await conn.send("anyone there?")
    finally:
        await conn.close()
    await asyncio.sleep(0)
