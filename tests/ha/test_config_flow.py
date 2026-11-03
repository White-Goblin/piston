"""Setting up, pairing, re-pairing and configuring the integration through Home Assistant's real flow manager."""
from ipaddress import ip_address

import pytest
from homeassistant.config_entries import SOURCE_ZEROCONF, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.decky_home_assistant.const import DEFAULT_OPTIONS, DOMAIN

from conftest import DEVICE_ID, SECRET


def announcement(device, **properties):
    props = {"device_id": DEVICE_ID, "name": "Box", "proto": "2", "paired": "0", **properties}
    props = {k: v for k, v in props.items() if v is not None}
    return ZeroconfServiceInfo(ip_address=ip_address("127.0.0.1"), ip_addresses=[ip_address("127.0.0.1")], port=device.port,
                               hostname="box.local.", type="_deckyha._tcp.local.", name=f"{DEVICE_ID}._deckyha._tcp.local.",
                               properties=props)


async def discover(hass, device, **properties):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_ZEROCONF}, data=announcement(device, **properties))


async def finish(hass, result):
    """Unload the entry a finished flow created, so nothing keeps running after the test."""
    entry = result["result"]
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    return entry


# --- discovery and pairing ----------------------------------------------------------------------

async def test_a_discovered_device_is_paired_with_the_code_shown_on_it(hass, unpaired_device, wait_until):
    result = await discover(hass, unpaired_device)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "confirm")
    assert result["description_placeholders"] == {"name": "Box"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "pair")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": unpaired_device.pairing.code.code})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Box"
    assert result["data"]["secret"] == unpaired_device.box["secret"].hex()          # the same secret on both sides
    assert (result["data"]["device_id"], result["data"]["host"], result["data"]["port"]) == (DEVICE_ID, "127.0.0.1", unpaired_device.port)
    assert result["options"] == DEFAULT_OPTIONS
    entry = result["result"]
    await wait_until(lambda: entry.state is ConfigEntryState.LOADED and entry.runtime_data.connected)
    await finish(hass, result)


async def test_wrong_codes_are_refused_and_too_many_lock_the_device(hass, unpaired_device):
    result = await discover(hass, unpaired_device)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    right = unpaired_device.pairing.code.code
    wrong = "000000" if right != "000000" else "111111"
    for _ in range(4):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": wrong})
        assert result["errors"] == {"base": "wrong_code"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": wrong})
    assert result["errors"] == {"base": "wrong_code"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": right})   # locked out now, even with the right one
    assert result["errors"] == {"base": "code_locked"}
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_a_device_that_is_already_paired_elsewhere_is_not_offered(hass, device):
    result = await discover(hass, device, paired="1")
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_paired")


async def test_an_announcement_without_a_device_id_is_not_supported(hass, device):
    result = await discover(hass, device, device_id=None)
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "not_supported")


async def test_a_known_device_that_moved_keeps_its_entry_and_gets_the_new_address(hass, config_entry, device):
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, "host": "192.168.1.250", "port": 9})
    result = await discover(hass, device, paired="1")
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_configured")
    assert (config_entry.data["host"], config_entry.data["port"]) == ("127.0.0.1", device.port)


# --- adding by address -----------------------------------------------------------------------

async def test_adding_by_address_then_pairing(hass, unpaired_device, wait_until):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "user")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": " 127.0.0.1 ", "port": unpaired_device.port})
    assert result["step_id"] == "pair"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": unpaired_device.pairing.code.code})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"]["host"] == "127.0.0.1"
    await finish(hass, result)


async def test_adding_by_address_shows_an_error_for_an_unreachable_address(hass, unpaired_device):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "127.0.0.1", "port": 1})
    assert (result["type"], result["errors"]) == (FlowResultType.FORM, {"base": "cannot_connect"})
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_adding_an_already_paired_device_by_address_is_refused(hass, device):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "127.0.0.1", "port": device.port})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_paired")


async def test_the_same_device_cannot_be_added_twice(hass, config_entry, device):
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "127.0.0.1", "port": device.port})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_configured")


# --- the device was reset: pair again ----------------------------------------------------------------

async def test_a_rejected_secret_starts_re_pairing_which_restores_the_connection(hass, unpaired_device, wait_until):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_ID, title="Box", options=dict(DEFAULT_OPTIONS),
                            data={"device_id": DEVICE_ID, "secret": SECRET.hex(), "host": "127.0.0.1", "port": unpaired_device.port, "name": "Box"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)                    # the device forgot the pairing
    await wait_until(lambda: entry.runtime_data.problem == "auth_failed")
    await hass.async_block_till_done()
    assert hass.states.get("media_player.box").state == "unavailable"               # a problem, not "off"
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["source"] == "reauth"
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    assert result["step_id"] == "pair"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": unpaired_device.pairing.code.code})
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reauth_successful")
    await hass.async_block_till_done()
    assert entry.data["secret"] == unpaired_device.box["secret"].hex() != SECRET.hex()
    await wait_until(lambda: entry.runtime_data.connected and entry.runtime_data.problem is None)
    assert await hass.config_entries.async_unload(entry.entry_id)


# --- options, removal, real transport -------------------------------------------------------------------

async def test_options_are_saved_as_numbers_and_the_sensor_interval_reaches_the_device(hass, config_entry, loaded, device, wait_until):
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "init")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "turn_off": "shutdown", "sensor_interval": "300", "pip_cameras": ["camera.door"], "pip_position": "bottom-left",
        "pip_size": "large", "pip_duration": "60", "pip_mute": False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options["sensor_interval"] == 300 and config_entry.options["pip_duration"] == 60
    assert (config_entry.options["turn_off"], config_entry.options["pip_cameras"]) == ("shutdown", ["camera.door"])
    await wait_until(lambda: {"seconds": 300} in device.sent("set_sensor_interval"))


async def test_removing_the_entry_unpairs_the_device_and_deletes_its_stored_state(hass, hass_storage, config_entry, loaded, device):
    key = f"decky_home_assistant.{config_entry.entry_id}"
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert key in hass_storage
    assert await hass.config_entries.async_remove(config_entry.entry_id) is not None
    await hass.async_block_till_done()
    assert len(device.sent("unpair")) == 1 and device.box["secret"] == b""
    assert key not in hass_storage


async def test_removing_an_entry_whose_device_is_unreachable_still_works(hass, config_entry, device):
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, "port": 1})
    await hass.config_entries.async_remove(config_entry.entry_id)
    assert hass.config_entries.async_get_entry(config_entry.entry_id) is None


async def test_the_real_aiohttp_websocket_client_connects_and_runs_commands(hass, real_transport, config_entry, device, wait_until):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    coordinator = config_entry.runtime_data
    await wait_until(lambda: coordinator.connected)
    await hass.async_block_till_done()
    await hass.services.async_call("button", "press", {"entity_id": "button.box_restart_device"}, blocking=True)
    assert device.sent("power") == [{"action": "restart"}]
    device.push("sensors", {"cpu_load": 11})
    await wait_until(lambda: getattr(hass.states.get("sensor.box_cpu_load"), "state", None) == "11")
    assert await hass.config_entries.async_unload(config_entry.entry_id)
