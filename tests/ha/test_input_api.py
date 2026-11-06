"""The remote card's websocket API in a real Home Assistant: access rules, status stream, forwarding and release."""
import pytest
from homeassistant.helpers import device_registry as dr

from custom_components.piston.const import DOMAIN, OPT_ALLOW_ALL_USERS

from conftest import DEVICE_ID

INPUT_ON = {"device_id": DEVICE_ID, "name": "Box", "protocol": 3, "session": "game", "input": {"enabled": True}}


@pytest.fixture
def ha_device_id(hass, loaded, config_entry):
    return dr.async_get(hass).async_get_device_by_identifier((DOMAIN, DEVICE_ID), config_entry.entry_id).id


@pytest.fixture
async def admin(hass, hass_ws_client):
    return await hass_ws_client(hass)


@pytest.fixture
async def user(hass, hass_ws_client, hass_read_only_user):
    refresh_token = await hass.auth.async_create_refresh_token(hass_read_only_user, "https://example.test")
    return await hass_ws_client(hass, hass.auth.async_create_access_token(refresh_token))


async def subscribe(ws, device_id, msg_id=1):
    await ws.send_json({"id": msg_id, "type": "piston/input/subscribe", "device_id": device_id})
    return await ws.receive_json()


async def next_status(ws):
    msg = await ws.receive_json()
    assert msg["type"] == "event", msg
    return msg["event"]["status"]


async def barrier(ws, msg_id=99):
    """Round-trip a ping: everything the server was going to answer before it has arrived by now."""
    await ws.send_json({"id": msg_id, "type": "ping"})
    msg = await ws.receive_json()
    assert (msg["id"], msg["type"]) == (msg_id, "pong"), msg


async def test_an_admin_gets_the_current_status_then_changes(hass, loaded, device, ha_device_id, admin, wait_until):
    reply = await subscribe(admin, ha_device_id)
    assert reply["success"] is True
    assert await next_status(admin) == "no_capability"          # the fake device's group has no `input` field yet
    device.push("device", INPUT_ON)
    assert await next_status(admin) == "ready"
    device.push("device", {**INPUT_ON, "session": "desktop"})
    assert await next_status(admin) == "desktop"
    device.push("device", {**INPUT_ON, "input": {"enabled": False}})
    assert await next_status(admin) == "disabled"


async def test_the_piston_device_id_works_too(hass, loaded, device, admin):
    assert (await subscribe(admin, DEVICE_ID))["success"] is True


async def test_an_unknown_device_is_not_found(hass, loaded, admin):
    reply = await subscribe(admin, "nope")
    assert (reply["success"], reply["error"]["code"]) == (False, "not_found")
    await admin.send_json({"id": 2, "type": "piston/input/send", "device_id": "nope", "events": [{"k": "release"}]})
    assert (await admin.receive_json())["error"]["code"] == "not_found"


async def test_a_non_admin_is_refused_until_the_option_is_on(hass, loaded, device, ha_device_id, user, config_entry):
    reply = await subscribe(user, ha_device_id)
    assert (reply["success"], reply["error"]["code"]) == (False, "unauthorized")
    await user.send_json({"id": 2, "type": "piston/input/send", "device_id": ha_device_id, "events": [{"k": "release"}]})
    assert (await user.receive_json())["error"]["code"] == "unauthorized"
    assert device.inputs == []
    hass.config_entries.async_update_entry(config_entry, options={**config_entry.options, OPT_ALLOW_ALL_USERS: True})
    assert (await subscribe(user, ha_device_id, 3))["success"] is True
    device.push("device", INPUT_ON)


async def test_authorization_is_checked_on_every_call(hass, loaded, device, ha_device_id, user, config_entry):
    hass.config_entries.async_update_entry(config_entry, options={**config_entry.options, OPT_ALLOW_ALL_USERS: True})
    await user.send_json({"id": 1, "type": "piston/input/send", "device_id": ha_device_id, "events": [{"k": "key", "key": "Enter"}]})
    await barrier(user)
    for _ in range(100):
        if device.inputs:
            break
        await hass.async_block_till_done()
    assert device.inputs == [[{"k": "key", "key": "Enter"}]]
    hass.config_entries.async_update_entry(config_entry, options={**config_entry.options, OPT_ALLOW_ALL_USERS: False})
    await user.send_json({"id": 100, "type": "piston/input/send", "device_id": ha_device_id, "events": [{"k": "key", "key": "Enter"}]})
    assert (await user.receive_json())["error"]["code"] == "unauthorized"


async def test_events_reach_the_device_and_a_good_send_gets_no_reply(hass, loaded, device, ha_device_id, admin, wait_until):
    events = [{"k": "move", "dx": 3, "dy": -2}, {"k": "click", "b": "l"}, {"k": "text", "s": "hi"}]
    await admin.send_json({"id": 1, "type": "piston/input/send", "device_id": ha_device_id, "events": events})
    await barrier(admin)                      # the next message is the pong: no result was sent for the send
    await wait_until(lambda: device.inputs)
    assert [e for batch in device.inputs for e in batch] == events


async def test_bad_events_are_refused_and_not_forwarded(hass, loaded, device, ha_device_id, admin):
    await admin.send_json({"id": 1, "type": "piston/input/send", "device_id": ha_device_id, "events": [{"k": "key", "key": "F13"}]})
    reply = await admin.receive_json()
    assert (reply["success"], reply["error"]["code"]) == (False, "invalid_format")
    await admin.send_json({"id": 2, "type": "piston/input/send", "device_id": ha_device_id, "events": []})
    assert (await admin.receive_json())["error"]["code"] == "invalid_format"
    assert device.inputs == []


async def test_closing_the_subscription_sends_release_to_the_device(hass, loaded, device, ha_device_id, admin, wait_until):
    reply = await subscribe(admin, ha_device_id)
    assert reply["success"] is True
    await next_status(admin)
    await admin.send_json({"id": 5, "type": "unsubscribe_events", "subscription": 1})
    assert (await admin.receive_json())["success"] is True
    await wait_until(lambda: device.inputs)
    assert device.inputs[-1] == [{"k": "release"}]


async def test_dropping_the_websocket_sends_release_to_the_device(hass, loaded, device, ha_device_id, admin, wait_until):
    await subscribe(admin, ha_device_id)
    await next_status(admin)
    await admin.close()
    await wait_until(lambda: device.inputs)
    assert device.inputs[-1] == [{"k": "release"}]


async def test_input_for_an_unreachable_device_is_dropped_quietly(hass, loaded, device, ha_device_id, admin):
    await device.stop()
    await hass.async_block_till_done()
    await admin.send_json({"id": 1, "type": "piston/input/send", "device_id": ha_device_id, "events": [{"k": "release"}]})
    await barrier(admin)


async def test_the_option_is_in_the_options_flow_and_off_by_default(hass, loaded, config_entry):
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    schema = {str(k): k for k in result["data_schema"].schema}
    assert OPT_ALLOW_ALL_USERS in schema
    assert schema[OPT_ALLOW_ALL_USERS].default() is False
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "turn_off": "sleep", "sensor_interval": "60", "pip_position": "top-right", "pip_size": "medium",
        "pip_duration": "30", "pip_mute": True, OPT_ALLOW_ALL_USERS: True})
    assert result["data"][OPT_ALLOW_ALL_USERS] is True
