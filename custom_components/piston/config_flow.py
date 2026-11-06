"""Config flow: discovery (mDNS) or a typed address, then pairing with the code shown on the device."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from ._compat import vol
from .client import ConnectionClosed, PairingError, pair, probe
from .const import (
    CONF_DEVICE_ID, CONF_HOST, CONF_NAME, CONF_PORT, CONF_SECRET, DEFAULT_OPTIONS, DEFAULT_PORT, DOMAIN,
    OPT_ALLOW_ALL_USERS, OPT_PIP_CAMERAS, OPT_PIP_DURATION, OPT_PIP_MUTE, OPT_PIP_POSITION, OPT_PIP_SIZE, OPT_SENSOR_INTERVAL, OPT_TURN_OFF,
    PIP_DURATIONS, PIP_POSITIONS, PIP_SIZES, SENSOR_INTERVALS, TURN_OFF_MODES,
)

_LOGGER = logging.getLogger(__name__)

PAIR_ERRORS = {
    "wrong_code": "wrong_code",
    "expired": "code_expired",
    "locked": "code_locked",
    "version_skew": "version_skew",
    "already_paired": "already_paired",
}


def make_transport(hass):
    """The websocket transport for this Home Assistant (replaced in tests)."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .transport import AiohttpTransport

    return AiohttpTransport(async_get_clientsession(hass))


class DeckyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._host = ""
        self._port = DEFAULT_PORT
        self._device_id = ""
        self._name = ""
        self._reauth_entry: ConfigEntry | None = None

    # --- finding the device ------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Fallback when mDNS does not reach Home Assistant (for example another VLAN): type the address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host, self._port = user_input[CONF_HOST].strip(), user_input[CONF_PORT]
            try:
                hello = await probe(make_transport(self.hass), self._host, self._port)
            except (OSError, ConnectionClosed, asyncio.TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                self._device_id, self._name = hello["device_id"], str(hello.get("name") or "")
                await self.async_set_unique_id(self._device_id)
                self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
                if hello.get("paired"):
                    return self.async_abort(reason="already_paired")
                return await self.async_step_pair()
        schema = vol.Schema({vol.Required(CONF_HOST): str, vol.Required(CONF_PORT, default=DEFAULT_PORT): int})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        props = discovery_info.properties
        device_id = props.get("device_id")
        if not device_id:
            return self.async_abort(reason="not_supported")
        self._host, self._port = str(discovery_info.host), discovery_info.port or DEFAULT_PORT
        self._device_id, self._name = device_id, str(props.get("name") or "Decky device")
        await self.async_set_unique_id(device_id)
        # A known device that moved: keep the entry, update where it lives
        self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
        if str(props.get("paired")) == "1":
            return self.async_abort(reason="already_paired")
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_pair()
        return self.async_show_form(step_id="confirm", description_placeholders={"name": self._name})

    # --- pairing -----------------------------------------------------------

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                result = await pair(make_transport(self.hass), self._host, self._port, user_input["code"].strip())
            except PairingError as e:
                errors["base"] = PAIR_ERRORS.get(e.reason, "unknown")
                if e.reason == "already_paired":
                    return self.async_abort(reason="already_paired")
            except (OSError, ConnectionClosed, asyncio.TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                if result.device_id != self._device_id:
                    return self.async_abort(reason="wrong_device")
                data = {CONF_DEVICE_ID: result.device_id, CONF_SECRET: result.secret.hex(),
                        CONF_HOST: self._host, CONF_PORT: self._port, CONF_NAME: result.name or self._name}
                if self._reauth_entry is not None:
                    return self.async_update_reload_and_abort(self._reauth_entry, data_updates=data)
                return self.async_create_entry(title=data[CONF_NAME] or "Decky device", data=data,
                                               options=dict(DEFAULT_OPTIONS))
        schema = vol.Schema({vol.Required("code"): str})
        return self.async_show_form(step_id="pair", data_schema=schema, errors=errors,
                                    description_placeholders={"name": self._name})

    # --- re-pairing --------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        self._host, self._port = entry_data[CONF_HOST], entry_data.get(CONF_PORT, DEFAULT_PORT)
        self._device_id, self._name = entry_data[CONF_DEVICE_ID], entry_data.get(CONF_NAME, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_pair()
        return self.async_show_form(step_id="reauth_confirm", description_placeholders={"name": self._name})

    # --- changing the address ----------------------------------------------

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Point the entry at a new address (the device moved and mDNS does not reach Home Assistant).
        The pairing is kept: only the host and port change, and only for the same device."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = user_input[CONF_HOST].strip(), user_input[CONF_PORT]
            try:
                hello = await probe(make_transport(self.hass), host, port)
            except (OSError, ConnectionClosed, asyncio.TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                if hello["device_id"] != entry.data[CONF_DEVICE_ID]:
                    return self.async_abort(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data_updates={CONF_HOST: host, CONF_PORT: port})
        current_host = (user_input or entry.data).get(CONF_HOST, entry.data[CONF_HOST])
        current_port = (user_input or entry.data).get(CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT))
        schema = vol.Schema({vol.Required(CONF_HOST, default=current_host): str, vol.Required(CONF_PORT, default=current_port): int})
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors,
                                    description_placeholders={"name": entry.data.get(CONF_NAME) or "the device"})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DeckyOptionsFlow()


class DeckyOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            # Selectors give strings; numbers are stored as numbers
            for key in (OPT_SENSOR_INTERVAL, OPT_PIP_DURATION):
                user_input[key] = int(user_input[key])
            return self.async_create_entry(data=user_input)
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        def choice(values, key: str):
            return selector.SelectSelector(selector.SelectSelectorConfig(
                options=[str(v) for v in values], translation_key=key, mode=selector.SelectSelectorMode.DROPDOWN))

        schema = vol.Schema({
            vol.Required(OPT_TURN_OFF, default=current[OPT_TURN_OFF]): choice(TURN_OFF_MODES, "turn_off"),
            vol.Required(OPT_SENSOR_INTERVAL, default=str(current[OPT_SENSOR_INTERVAL])): choice(SENSOR_INTERVALS, "sensor_interval"),
            vol.Optional(OPT_PIP_CAMERAS, default=current[OPT_PIP_CAMERAS]): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="camera", multiple=True)),
            vol.Required(OPT_PIP_POSITION, default=current[OPT_PIP_POSITION]): choice(PIP_POSITIONS, "pip_position"),
            vol.Required(OPT_PIP_SIZE, default=current[OPT_PIP_SIZE]): choice(PIP_SIZES, "pip_size"),
            vol.Required(OPT_PIP_DURATION, default=str(current[OPT_PIP_DURATION])): choice(PIP_DURATIONS, "pip_duration"),
            vol.Required(OPT_PIP_MUTE, default=current[OPT_PIP_MUTE]): bool,
            vol.Required(OPT_ALLOW_ALL_USERS, default=current[OPT_ALLOW_ALL_USERS]): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
