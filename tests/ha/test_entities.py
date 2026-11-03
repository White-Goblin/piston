"""Every platform through Home Assistant's real service calls, against the real protocol server on loopback."""
from unittest import mock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.decky_home_assistant.const import DOMAIN

APPS = [{"appid": "250900", "name": "Isaac", "kind": "steam"}, {"appid": "2451505133", "name": "Jellyfin", "kind": "shortcut"}]
RUNNING = {"apps": APPS, "running": True, "appid": "250900", "name": "Isaac", "screen": "game"}


async def call(hass, domain, service, entity_id, **data):
    await hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)


async def settle(hass, wait_until, check):
    await wait_until(check)
    await hass.async_block_till_done()


# --- media player -------------------------------------------------------------------------

async def test_media_player_follows_the_device(hass, loaded, device, state, wait_until):
    assert state("media_player.box") == "idle"
    device.push("apps", RUNNING)
    await settle(hass, wait_until, lambda: state("media_player.box") == "playing")
    attributes = hass.states.get("media_player.box").attributes
    assert (attributes["source"], attributes["source_list"], attributes["app_name"]) == ("Isaac", ["Steam", "Isaac", "Jellyfin"], "Isaac")
    device.push("apps", {**RUNNING, "playback": "paused"})
    await settle(hass, wait_until, lambda: state("media_player.box") == "paused")
    device.event("going_to_sleep", {"reason": "suspend"})
    await settle(hass, wait_until, lambda: state("media_player.box") == "standby")
    assert state("sensor.box_steam_screen") == "unavailable"          # everything but the media player is unavailable asleep
    device.event("resumed")
    await settle(hass, wait_until, lambda: state("media_player.box") == "paused")
    device.event("shutting_down", {"reason": "shutdown"})
    await settle(hass, wait_until, lambda: state("media_player.box") == "off")
    assert state("switch.box_desktop_mode") == "unavailable"


async def test_sources_launch_apps_and_steam_quits_the_running_one(hass, loaded, device, wait_until):
    device.push("apps", RUNNING)
    await wait_until(lambda: hass.states.get("media_player.box").state == "playing")
    await call(hass, "media_player", "select_source", "media_player.box", source="Jellyfin")
    await call(hass, "media_player", "select_source", "media_player.box", source="Steam")
    assert device.sent("launch") == [{"appid": "2451505133"}]
    assert len(device.sent("quit")) == 1
    with pytest.raises(ServiceValidationError):
        await call(hass, "media_player", "select_source", "media_player.box", source="Nowhere")


async def test_transport_keys(hass, loaded, device):
    for service, key in (("media_play", "play"), ("media_pause", "pause"), ("media_play_pause", "play_pause"),
                         ("media_next_track", "next"), ("media_previous_track", "previous")):
        await call(hass, "media_player", service, "media_player.box")
        assert device.sent("media_key")[-1] == {"key": key}


async def test_volume_through_the_system_mixer_and_through_cec(hass, loaded, device, wait_until):
    device.push("audio", {"mode": "system", "volume": 40, "muted": False})
    await wait_until(lambda: hass.states.get("media_player.box").attributes.get("volume_level") == 0.4)
    await call(hass, "media_player", "volume_up", "media_player.box")
    await call(hass, "media_player", "volume_down", "media_player.box")
    await call(hass, "media_player", "volume_set", "media_player.box", volume_level=0.3)
    await call(hass, "media_player", "volume_mute", "media_player.box", is_volume_muted=True)
    assert device.sent("audio") == [{"action": "set_volume", "value": 45}, {"action": "set_volume", "value": 35},
                                    {"action": "set_volume", "value": 30}, {"action": "set_mute", "value": True}]
    device.commands.clear()
    device.push("audio", {"mode": "cec"})                             # a TV over HDMI CEC: steps and a mute toggle only
    await wait_until(lambda: "volume_level" not in hass.states.get("media_player.box").attributes)
    await call(hass, "media_player", "volume_up", "media_player.box")
    await call(hass, "media_player", "volume_down", "media_player.box")
    await call(hass, "media_player", "volume_mute", "media_player.box", is_volume_muted=True)
    await call(hass, "media_player", "volume_mute", "media_player.box", is_volume_muted=True)     # already muted: no second toggle
    assert device.sent("audio") == [{"action": "volume_up"}, {"action": "volume_down"}, {"action": "mute"}]


async def test_turn_off_sleeps_or_shuts_down_as_configured(hass, config_entry, loaded, device, wait_until):
    await call(hass, "media_player", "turn_off", "media_player.box")
    hass.config_entries.async_update_entry(config_entry, options={**config_entry.options, "turn_off": "shutdown"})
    await hass.async_block_till_done()
    await call(hass, "media_player", "turn_off", "media_player.box")
    assert device.sent("power") == [{"action": "suspend"}, {"action": "shutdown"}]


async def test_turn_on_sends_wake_on_lan_only_when_the_device_is_not_already_awake(hass, loaded, device, wait_until):
    with mock.patch("custom_components.decky_home_assistant.media_player.send_magic_packet", mock.AsyncMock()) as wake:
        await call(hass, "media_player", "turn_on", "media_player.box")
        wake.assert_not_awaited()                                   # connected and awake
        device.event("going_to_sleep", {"reason": "suspend"})
        await wait_until(lambda: hass.states.get("media_player.box").state == "standby")
        await call(hass, "media_player", "turn_on", "media_player.box")      # available while asleep: this is what keeps Wake on LAN working
        wake.assert_awaited_once_with("aa:bb:cc:dd:ee:ff", "10.0.0.255")


async def test_turn_on_reports_a_failed_wake_packet(hass, loaded, device, wait_until):
    device.event("going_to_sleep", {"reason": "suspend"})
    await wait_until(lambda: hass.states.get("media_player.box").state == "standby")
    boom = mock.AsyncMock(side_effect=OSError("network unreachable"))
    with mock.patch("custom_components.decky_home_assistant.media_player.send_magic_packet", boom):
        with pytest.raises(HomeAssistantError, match="wake packet"):
            await call(hass, "media_player", "turn_on", "media_player.box")


async def test_failed_commands_become_the_right_kind_of_error(hass, loaded, device, wait_until):
    device.fail["launch"] = "bad_request"
    device.push("apps", RUNNING)
    await wait_until(lambda: hass.states.get("media_player.box").state == "playing")
    with pytest.raises(ServiceValidationError):                          # wrong input: the caller can fix it
        await call(hass, "media_player", "select_source", "media_player.box", source="Jellyfin")
    device.fail["launch"] = "internal"
    with pytest.raises(HomeAssistantError) as raised:                    # the action itself failed
        await call(hass, "media_player", "select_source", "media_player.box", source="Jellyfin")
    assert not isinstance(raised.value, ServiceValidationError)


async def test_commands_fail_cleanly_while_the_device_is_unreachable(hass, loaded, device, wait_until):
    await device.stop()
    await wait_until(lambda: not loaded.connected)
    with pytest.raises(HomeAssistantError, match="offline"):
        await call(hass, "media_player", "media_play", "media_player.box")


# --- switch, buttons, sensors, events -----------------------------------------------------------

async def test_desktop_mode_switch(hass, loaded, device, state, wait_until):
    assert state("switch.box_desktop_mode") == "off"
    await call(hass, "switch", "turn_on", "switch.box_desktop_mode")
    await call(hass, "switch", "turn_off", "switch.box_desktop_mode")
    assert device.sent("power") == [{"action": "desktop"}, {"action": "game"}]
    device.push("device", {**device.groups["device"], "session": "desktop"})
    await settle(hass, wait_until, lambda: state("switch.box_desktop_mode") == "on")


async def test_restart_buttons(hass, loaded, device):
    await call(hass, "button", "press", "button.box_restart_device")
    await call(hass, "button", "press", "button.box_restart_steam")
    assert device.sent("power") == [{"action": "restart"}, {"action": "restart_steam"}]


async def test_sensors_and_the_steam_screen(hass, loaded, device, state, wait_until):
    device.push("sensors", {"cpu_load": 7, "cpu_temp": 51.5})
    device.push("apps", {**RUNNING, "screen": "steam_over_game"})
    await settle(hass, wait_until, lambda: state("sensor.box_cpu_load") == "7")
    assert float(state("sensor.box_cpu_temperature")) == 51.5
    await settle(hass, wait_until, lambda: state("sensor.box_steam_screen") == "steam_over_game")
    assert state("binary_sensor.box_connectivity") == "on"
    assert state("sensor.box_pip_state") in ("unavailable", "idle")


async def test_controller_batteries_appear_and_stay_when_a_controller_disconnects(hass, loaded, device, state, wait_until):
    pad = {"name": "Pro 2", "battery": 52, "charging": True, "wireless": True, "stale": False, "updated": "t"}
    device.push("controllers", {"connected": {"aabbccdd": pad}, "seen": [{"serial": "aabbccdd", "name": "Pro 2"}]})
    await settle(hass, wait_until, lambda: state("sensor.box_pro_2_battery") == "52")
    assert state("binary_sensor.box_pro_2_charging") == "on"
    device.push("controllers", {"connected": {}, "seen": [{"serial": "aabbccdd", "name": "Pro 2"}]})
    await settle(hass, wait_until, lambda: state("sensor.box_pro_2_battery") == "unavailable")


async def test_a_toast_click_fires_the_event_entity(hass, loaded, device, state, wait_until):
    device.event("toast_click", {"id": "door", "at": 1700000000})
    await settle(hass, wait_until, lambda: state("event.box_toast_click") != "unknown")
    attributes = hass.states.get("event.box_toast_click").attributes
    assert (attributes["event_type"], attributes["id"], attributes["clicked_at"]) == ("clicked", "door", 1700000000)


# --- notifications and picture-in-picture -------------------------------------------------------

async def test_notify_and_the_show_toast_action(hass, loaded, device):
    await hass.services.async_call("notify", "send_message", {"entity_id": "notify.box_toast", "message": "Hi", "title": "Door"}, blocking=True)
    await hass.services.async_call(DOMAIN, "show_toast", {"entity_id": "notify.box_toast", "message": "Bell", "duration": 3,
                                                          "icon": "bell", "silent": True, "id": "b1"}, blocking=True)
    first, second = device.sent("toast")
    assert (first["message"], first["title"], first["duration"]) == ("Hi", "Door", 5000)
    assert (second["message"], second["duration"], second["icon"], second["silent"], second["id"]) == ("Bell", 3000, "bell", True, "b1")
    device.fail["toast"] = "bad_request"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "show_toast", {"entity_id": "notify.box_toast", "message": " "}, blocking=True)


async def test_pip_select_and_actions(hass, config_entry, loaded, device, state, wait_until):
    hass.config_entries.async_update_entry(config_entry, options={**config_entry.options, "pip_cameras": ["camera.door", "camera.drive"]})
    await hass.async_block_till_done()
    assert hass.states.get("select.box_pip_camera").attributes["options"] == ["Off", "camera.door", "camera.drive"]
    await call(hass, "select", "select_option", "select.box_pip_camera", option="camera.door")
    shown = device.sent("pip_show")
    assert shown[0]["camera"] == "camera.door" and shown[0]["session"]
    await hass.services.async_call(DOMAIN, "show_pip", {"entity_id": "select.box_pip_camera", "camera": "camera.drive", "position": "top-left",
                                                        "size": "large", "duration": 60, "mute": False}, blocking=True)
    assert device.sent("pip_show")[1]["camera"] == "camera.drive"
    device.push("pip", {"state": "playing", "entity_id": "camera.drive"})
    await settle(hass, wait_until, lambda: state("select.box_pip_camera") == "camera.drive")
    await call(hass, "select", "select_option", "select.box_pip_camera", option="Off")
    await hass.services.async_call(DOMAIN, "hide_pip", {"entity_id": "select.box_pip_camera"}, blocking=True)
    assert len(device.sent("pip_hide")) == 2
    with pytest.raises(ServiceValidationError):
        await call(hass, "select", "select_option", "select.box_pip_camera", option="camera.nowhere")
    device.fail["pip_show"] = "internal"
    with pytest.raises(HomeAssistantError):
        await call(hass, "select", "select_option", "select.box_pip_camera", option="camera.door")
