"""The plugin update entity, the auto-install switch and their Repairs, in a real Home Assistant."""
from unittest import mock

import pytest
from homeassistant.helpers import issue_registry as ir

import custom_components.piston as integration
from custom_components.piston import update_logic
from custom_components.piston.const import DOMAIN

from conftest import DEVICE_ID

UPDATE = "update.box_plugin_update"
AUTO = "switch.box_install_plugin_updates_automatically"
SHA = "e" * 64
_real_integration_version = integration._integration_version     # the autouse fixture below replaces them in tests
_real_text_fetcher = integration._text_fetcher


@pytest.fixture(autouse=True)
def lane_device(device):
    """The device advertises version 0.4.0 and the update lane; every update request lands in `device.updates`."""
    device.updates = []
    device.server._hello_extra = lambda: {"name": "Box", "plugin_version": "0.4.0", "lane": 1}
    device.server._on_update = device.updates.append
    return device


@pytest.fixture(autouse=True)
def release():
    """The integration is version 0.4.1 and GitHub serves a SHA256SUMS for it."""
    async def version(hass):
        return "0.4.1"

    def fetcher(hass):
        async def get(url):
            return f"{SHA}  {update_logic.zip_name('0.4.1')}\n"
        return get

    with mock.patch.object(integration, "_integration_version", version), mock.patch.object(integration, "_text_fetcher", fetcher):
        yield


async def test_a_device_on_an_older_plugin_is_updated_automatically(hass, loaded, device, state, wait_until):
    await wait_until(lambda: device.updates)
    request = device.updates[0]
    assert (request["version"], request["sha256"]) == ("0.4.1", SHA)
    assert request["url"] == update_logic.asset_url("0.4.1")
    assert state(UPDATE) == "on"                                  # 0.4.0 installed, 0.4.1 latest
    entity = hass.states.get(UPDATE)
    assert (entity.attributes["installed_version"], entity.attributes["latest_version"]) == ("0.4.0", "0.4.1")
    assert entity.attributes["release_url"].endswith("/releases/tag/v0.4.1")


async def test_progress_shows_on_the_entity_and_the_new_version_clears_it(hass, loaded, device, state, wait_until):
    await wait_until(lambda: device.updates)
    device.server.push_update_status("installing", "0.4.1")
    await wait_until(lambda: hass.states.get(UPDATE).attributes.get("in_progress"))
    device.server._hello_extra = lambda: {"name": "Box", "plugin_version": "0.4.1", "lane": 1}
    await device.server.disconnect(1001, "restarting")
    await wait_until(lambda: not loaded.connected)
    loaded.client.wake()
    await wait_until(lambda: loaded.plugin_version == "0.4.1")
    await hass.async_block_till_done()
    assert state(UPDATE) == "off"
    assert hass.states.get(UPDATE).attributes["update_state"] == "up_to_date"


async def test_with_automatic_updates_off_nothing_installs_until_install_is_pressed(hass, config_entry, device, wait_until, state):
    config_entry.add_to_hass(hass)
    with mock.patch.object(integration.DeckyCoordinator, "restore_update", lambda self, facts: setattr(self, "auto_update", False)):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
    coordinator = config_entry.runtime_data
    await wait_until(lambda: coordinator.connected and coordinator.plugin_version)
    await hass.async_block_till_done()
    assert state(AUTO) == "off"
    assert device.updates == []
    assert hass.states.get(UPDATE).attributes["update_state"] == "available"
    await hass.services.async_call("update", "install", {"entity_id": UPDATE}, blocking=True)
    await wait_until(lambda: device.updates)
    assert device.updates[0]["version"] == "0.4.1"
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_the_switch_is_a_config_entity_and_its_state_survives_a_reload(hass, loaded, config_entry, state, wait_until):
    from homeassistant.helpers import entity_registry as er
    assert state(AUTO) == "on"
    assert er.async_get(hass).async_get(AUTO).entity_category == "config"
    await hass.services.async_call("switch", "turn_off", {"entity_id": AUTO}, blocking=True)
    assert state(AUTO) == "off"
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await wait_until(lambda: config_entry.runtime_data.connected)
    await hass.async_block_till_done()
    assert state(AUTO) == "off"


async def test_a_failed_update_raises_a_repair_that_the_new_version_clears(hass, loaded, device, state, wait_until):
    await wait_until(lambda: device.updates)
    device.server.push_update_status("failed", "0.4.1", "hash")
    await wait_until(lambda: ir.async_get(hass).async_get_issue(DOMAIN, f"plugin_update_failed_{DEVICE_ID}"))
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"plugin_update_failed_{DEVICE_ID}")
    assert (issue.is_fixable, issue.severity) == (False, ir.IssueSeverity.WARNING)
    assert hass.states.get(UPDATE).attributes["update_state"] == "failed"
    assert hass.states.get(UPDATE).attributes["last_failure"] == {"version": "0.4.1", "reason": "hash"}
    device.server._hello_extra = lambda: {"name": "Box", "plugin_version": "0.4.1", "lane": 1}
    await device.server.disconnect(1001, "restarting")
    await wait_until(lambda: not loaded.connected)
    loaded.client.wake()
    await wait_until(lambda: ir.async_get(hass).async_get_issue(DOMAIN, f"plugin_update_failed_{DEVICE_ID}") is None)


async def test_a_protocol_mismatch_keeps_the_update_entity_available_and_updates_over_the_lane(hass, config_entry, device, state, wait_until):
    original = integration.DeckyClient
    config_entry.add_to_hass(hass)
    with mock.patch.object(integration, "DeckyClient", lambda *a, **kw: original(*a, own=(9, 9), **kw)):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
    coordinator = config_entry.runtime_data
    await wait_until(lambda: coordinator.skewed == "old" and device.updates)
    await hass.async_block_till_done()
    assert device.updates[0]["version"] == "0.4.1"                  # sent over the lane
    assert state(UPDATE) == "on"                                    # available while everything else is not
    assert state("media_player.box") == "unavailable"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"version_skew_old_{DEVICE_ID}") is None
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_the_real_integration_version_is_the_manifest_version(hass):
    import json
    import pathlib
    manifest = json.loads((pathlib.Path(integration.__file__).parent / "manifest.json").read_text())
    assert await _real_integration_version(hass) == manifest["version"]


async def test_the_checksum_file_is_fetched_with_a_timeout_through_the_shared_session(hass):
    import aiohttp

    calls = []

    class Response:
        def raise_for_status(self):
            calls.append("status")

        async def text(self):
            return "body"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def get(self, url, timeout):
            calls.append((url, timeout))
            return Response()

    with mock.patch.object(integration, "async_get_clientsession", lambda hass: Session()):
        assert await _real_text_fetcher(hass)("https://example.invalid/x") == "body"
    assert calls[0] == ("https://example.invalid/x", aiohttp.ClientTimeout(total=update_logic.SUMS_TIMEOUT))
    assert calls[1] == "status"


async def test_install_without_a_readable_checksum_is_an_error(hass, config_entry, device, wait_until):
    from homeassistant.exceptions import HomeAssistantError

    def fetcher(hass):
        async def get(url):
            raise OSError("no network")
        return get

    config_entry.add_to_hass(hass)
    with mock.patch.object(integration, "_text_fetcher", fetcher):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        coordinator = config_entry.runtime_data
        await wait_until(lambda: coordinator.connected and coordinator.plugin_version)
        await hass.async_block_till_done()
        assert hass.states.get(UPDATE).attributes["update_state"] == "cannot_determine"
        assert device.updates == []
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call("update", "install", {"entity_id": UPDATE}, blocking=True)
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_a_device_without_the_lane_is_never_sent_an_update(hass, config_entry, device, wait_until):
    device.server._hello_extra = lambda: {"name": "Box"}
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    coordinator = config_entry.runtime_data
    await wait_until(lambda: coordinator.connected)
    await hass.async_block_till_done()
    assert device.updates == []
    assert hass.states.get(UPDATE).attributes["update_state"] == "no_lane"
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_a_target_before_the_first_lane_release_is_not_sent(hass, config_entry, device, wait_until):
    async def old_version(hass):
        return "0.3.0"

    config_entry.add_to_hass(hass)
    with mock.patch.object(integration, "_integration_version", old_version):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        coordinator = config_entry.runtime_data
        await wait_until(lambda: coordinator.connected and coordinator.plugin_version)
        await hass.async_block_till_done()
    assert device.updates == []
    assert hass.states.get(UPDATE).attributes["update_state"] == "predates_lane"
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_a_failed_target_is_not_sent_again_on_reconnect(hass, loaded, device, wait_until):
    await wait_until(lambda: device.updates)
    device.server.push_update_status("failed", "0.4.1", "hash")
    await wait_until(lambda: loaded.update_latch)
    await device.server.disconnect(1001, "bye")
    await wait_until(lambda: not loaded.connected)
    loaded.client.wake()
    await wait_until(lambda: loaded.connected)
    await hass.async_block_till_done()
    assert len(device.updates) == 1


async def test_without_a_readable_integration_version_updates_are_off(hass):
    with mock.patch("homeassistant.loader.async_get_integration", side_effect=RuntimeError("no such integration")):
        assert await _real_integration_version(hass) is None


async def test_while_decky_waits_for_the_user_the_entity_and_repairs_say_what_to_do(hass, loaded, device, wait_until):
    await wait_until(lambda: device.updates)
    device.server.push_update_status("waiting_for_confirmation", "0.4.1")
    notice = f"plugin_update_confirm_{DEVICE_ID}"
    await wait_until(lambda: ir.async_get(hass).async_get_issue(DOMAIN, notice))
    entity = hass.states.get(UPDATE)
    assert entity.attributes["update_state"] == "waiting_for_confirmation"
    assert entity.attributes["in_progress"] is True
    assert "Decky Loader dialog" in entity.attributes["release_summary"]
    found = ir.async_get(hass).async_get_issue(DOMAIN, notice)
    assert (found.is_fixable, found.severity, found.translation_key) == (False, ir.IssueSeverity.WARNING, "plugin_update_confirm")
    device.server.push_update_status("failed", "0.4.1", "not_confirmed")
    await wait_until(lambda: ir.async_get(hass).async_get_issue(DOMAIN, notice) is None)
    failed = ir.async_get(hass).async_get_issue(DOMAIN, f"plugin_update_failed_{DEVICE_ID}")
    assert "not approved" in failed.translation_placeholders["reason"]
    assert hass.states.get(UPDATE).attributes["release_summary"] is None
