"""Tiny stand-ins for the parts of Home Assistant the integration imports.

Installed only when the real `homeassistant` package is not importable, so the stdlib
unittest suite runs anywhere. Add names here as integration code needs them; keep it small.
"""
import importlib.util
import sys
import types
from dataclasses import dataclass, field


def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    parent, _, child = name.rpartition(".")
    if parent in sys.modules:
        setattr(sys.modules[parent], child, mod)
    return mod


def callback(func):
    return func


class ConfigEntry:
    def __init__(self, data=None, options=None, entry_id="entry", unique_id=None, domain="decky_home_assistant",
                 title=""):
        self.data, self.options, self.entry_id = dict(data or {}), dict(options or {}), entry_id
        self.unique_id, self.domain, self.title = unique_id, domain, title
        self.update_listeners = []
        self.runtime_data = None
        self.reauth_started = False

    def async_on_unload(self, func):
        return func

    def add_update_listener(self, listener):
        self.update_listeners.append(listener)
        return lambda: None

    def async_start_reauth(self, hass):
        self.reauth_started = True


class ConfigEntries:
    def __init__(self):
        self.entries = []
        self.reloaded = []
        self.forwarded = []

    def async_entries(self, domain=None):
        return [e for e in self.entries if domain in (None, e.domain)]

    def async_get_entry(self, entry_id):
        return next((e for e in self.entries if e.entry_id == entry_id), None)

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded.append(list(platforms))

    async def async_unload_platforms(self, entry, platforms):
        return True

    async def async_reload(self, entry_id):
        self.reloaded.append(entry_id)

    def async_update_entry(self, entry, data=None, options=None, **kw):
        if data is not None:
            entry.data = dict(data)
        if options is not None:
            entry.options = dict(options)


class Bus:
    """Only what the integration uses: listen once for an event, and tests can fire it."""

    def __init__(self):
        self.once = {}

    def async_listen_once(self, event, callback):
        self.once.setdefault(event, []).append(callback)
        return lambda: self.once.get(event, []).remove(callback) if callback in self.once.get(event, []) else None

    async def fire(self, event):
        import asyncio
        for callback in self.once.pop(event, []):
            result = callback(None)
            if asyncio.iscoroutine(result):
                await result
        await asyncio.sleep(0)


class HomeAssistant:
    def __init__(self):
        self.data = {}
        self.config_entries = ConfigEntries()
        self.bus_events = []
        self.bus = Bus()

    def async_create_task(self, coro):
        import asyncio
        return asyncio.get_running_loop().create_task(coro)


class Timers:
    """homeassistant.helpers.event.async_track_time_interval: remembers (action, interval); tests call the action."""
    tracked = []

    @classmethod
    def async_track_time_interval(cls, hass, action, interval):
        entry = (action, interval)
        cls.tracked.append(entry)
        return lambda: cls.tracked.remove(entry) if entry in cls.tracked else None


class Store:
    """homeassistant.helpers.storage.Store with an in-memory "disk" shared by all instances (clear it with reset())."""
    files = {}       # key -> {"version": int, "data": ...}  or an Exception to raise when read (a corrupt file)
    writes = []      # (key, data) for every real write
    delays = []      # (key, delay) for every async_delay_save call
    removed = []

    @classmethod
    def reset(cls):
        cls.files, cls.writes, cls.delays, cls.removed = {}, [], [], []

    def __init__(self, hass, version, key, private=False, *, atomic_writes=False, minor_version=1):
        self.hass, self.version, self.key = hass, version, key
        self._pending = None

    async def async_load(self):
        import copy
        found = Store.files.get(self.key)
        if found is None:
            return None
        if isinstance(found, Exception):
            raise found
        if found["version"] != self.version:
            return await self._async_migrate_func(found["version"], 1, copy.deepcopy(found["data"]))
        return copy.deepcopy(found["data"])

    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        raise NotImplementedError

    async def async_save(self, data):
        import copy
        Store.files[self.key] = {"version": self.version, "data": copy.deepcopy(data)}
        Store.writes.append((self.key, copy.deepcopy(data)))

    def async_delay_save(self, data_func, delay=0):
        self._pending = data_func
        Store.delays.append((self.key, delay))

    async def fire_delay(self):
        """What Home Assistant does when the delay timer ends (tests call this instead of waiting)."""
        if self._pending is not None:
            func, self._pending = self._pending, None
            await self.async_save(func())

    async def async_remove(self):
        self._pending = None
        Store.files.pop(self.key, None)
        Store.removed.append(self.key)


class AbortFlow(Exception):
    def __init__(self, reason, description_placeholders=None):
        super().__init__(reason)
        self.reason = reason


class ConfigFlow:
    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.domain = domain

    hass = None
    unique_id = None

    @property
    def context(self):
        if not hasattr(self, "_context"):
            self._context = {}
        return self._context

    async def async_set_unique_id(self, unique_id, raise_on_progress=True):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self, updates=None, reload_on_update=True):
        for entry in self.hass.config_entries.async_entries(self.domain):
            if entry.unique_id == self.unique_id:
                if updates:
                    entry.data = {**entry.data, **updates}
                raise AbortFlow("already_configured")

    def async_show_form(self, *, step_id, data_schema=None, errors=None, description_placeholders=None):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors or {},
                "description_placeholders": description_placeholders or {}}

    def async_create_entry(self, *, title, data, options=None):
        return {"type": "create_entry", "title": title, "data": data, "options": options or {}}

    def async_abort(self, *, reason, description_placeholders=None):
        return {"type": "abort", "reason": reason}

    def async_update_reload_and_abort(self, entry, *, data_updates=None, **kw):
        entry.data = {**entry.data, **(data_updates or {})}
        self.hass.config_entries.reloaded.append(entry.entry_id)
        return {"type": "abort", "reason": "reauth_successful"}


class OptionsFlow:
    config_entry = None

    def async_show_form(self, *, step_id, data_schema=None, errors=None, description_placeholders=None):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors or {}}

    def async_create_entry(self, *, title="", data):
        return {"type": "create_entry", "data": data}


@dataclass
class ZeroconfServiceInfo:
    host: str
    port: int
    properties: dict = field(default_factory=dict)
    name: str = ""
    hostname: str = ""
    type: str = "_deckyha._tcp.local."


class _Marker:
    def __init__(self, key, default=None):
        self.key, self.default = key, default

    def __hash__(self):
        return hash(self.key)

    def __eq__(self, other):
        return getattr(other, "key", other) == self.key


class Required(_Marker):
    pass


class Optional(_Marker):
    pass


class In:
    def __init__(self, container):
        self.container = container


class Schema:
    def __init__(self, schema):
        self.schema = schema


class Entity:
    _attr_has_entity_name = False
    _attr_unique_id = None
    _attr_device_info = None
    _attr_entity_category = None
    _attr_device_class = None

    def __init__(self):
        self._removers = []
        self.writes = 0

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def device_info(self):
        return self._attr_device_info

    @property
    def available(self):
        return True

    def async_write_ha_state(self):
        self.writes = getattr(self, "writes", 0) + 1

    def async_on_remove(self, func):
        self.__dict__.setdefault("_removers", []).append(func)

    async def async_will_remove_from_hass(self):
        for func in self.__dict__.get("_removers", []):
            func()


class BinarySensorEntity(Entity):
    pass


class DeviceInfo(dict):
    pass


class _Enum:
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return self.value


class IssueRegistry:
    created, deleted = [], []


def async_create_issue(hass, domain, issue_id, **kw):
    IssueRegistry.created.append((issue_id, kw))


def async_delete_issue(hass, domain, issue_id):
    IssueRegistry.deleted.append(issue_id)


import enum


class MediaPlayerEntityFeature(enum.IntFlag):
    PAUSE = 1
    SEEK = 2
    VOLUME_SET = 4
    VOLUME_MUTE = 8
    PREVIOUS_TRACK = 16
    NEXT_TRACK = 32
    TURN_ON = 128
    TURN_OFF = 256
    PLAY_MEDIA = 512
    VOLUME_STEP = 1024
    SELECT_SOURCE = 2048
    STOP = 4096
    PLAY = 16384


class MediaPlayerState(enum.StrEnum):
    OFF = "off"
    ON = "on"
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    STANDBY = "standby"


class MediaPlayerEntity(Entity):
    pass


class HomeAssistantError(Exception):
    pass


class SwitchEntity(Entity):
    pass


class ButtonEntity(Entity):
    pass


class SensorEntity(Entity):
    pass


class NotifyEntity(Entity):
    pass


class EventEntity(Entity):
    def __init__(self):
        super().__init__()
        self.triggered = []

    def _trigger_event(self, event_type, event_attributes=None):
        self.__dict__.setdefault("triggered", []).append((event_type, event_attributes))


class _Platform:
    def __init__(self):
        self.services = {}

    def async_register_entity_service(self, name, schema, func):
        self.services[name] = (schema, func)


PLATFORM = _Platform()


class In:
    def __init__(self, container):
        self.container = container


class SelectEntity(Entity):
    pass


class _Cfg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Sel:
    def __init__(self, config=None):
        self.config = config


def install() -> None:
    try:
        if importlib.util.find_spec("homeassistant") is not None:
            return
    except (ImportError, ValueError):
        pass
    _module("homeassistant")
    _module("homeassistant.core", HomeAssistant=HomeAssistant, callback=callback)
    _module("homeassistant.config_entries", ConfigEntry=ConfigEntry, ConfigFlow=ConfigFlow, ConfigFlowResult=dict,
            OptionsFlow=OptionsFlow)
    _module("homeassistant.data_entry_flow", AbortFlow=AbortFlow)
    _module("homeassistant.helpers")
    _module("homeassistant.helpers.service_info")
    _module("homeassistant.helpers.service_info.zeroconf", ZeroconfServiceInfo=ZeroconfServiceInfo)
    _module("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
    _module("homeassistant.helpers.entity", Entity=Entity)
    _module("homeassistant.helpers.device_registry", CONNECTION_NETWORK_MAC="mac", DeviceInfo=DeviceInfo)
    _module("homeassistant.helpers.issue_registry", async_create_issue=async_create_issue,
            async_delete_issue=async_delete_issue, IssueSeverity=types.SimpleNamespace(ERROR="error", WARNING="warning"),
            IssueRegistry=IssueRegistry)
    sys.modules["homeassistant.helpers"].issue_registry = sys.modules["homeassistant.helpers.issue_registry"]
    _module("homeassistant.const", EntityCategory=types.SimpleNamespace(DIAGNOSTIC="diagnostic", CONFIG="config"),
            EVENT_HOMEASSISTANT_STOP="homeassistant_stop")
    _module("homeassistant.helpers.storage", Store=Store)
    _module("homeassistant.helpers.event", async_track_time_interval=Timers.async_track_time_interval)
    _module("homeassistant.components")
    _module("homeassistant.components.binary_sensor", BinarySensorEntity=BinarySensorEntity,
            BinarySensorDeviceClass=types.SimpleNamespace(CONNECTIVITY="connectivity"))
    _module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
    _module("homeassistant.components.media_player", MediaPlayerEntity=MediaPlayerEntity,
            MediaPlayerEntityFeature=MediaPlayerEntityFeature, MediaPlayerState=MediaPlayerState,
            MediaPlayerDeviceClass=types.SimpleNamespace(TV="tv"))
    _module("homeassistant.components.switch", SwitchEntity=SwitchEntity)
    _module("homeassistant.components.button", ButtonEntity=ButtonEntity,
            ButtonDeviceClass=types.SimpleNamespace(RESTART="restart"))
    _module("homeassistant.components.sensor", SensorEntity=SensorEntity,
            SensorDeviceClass=types.SimpleNamespace(ENUM="enum", BATTERY="battery"))
    _module("homeassistant.components.notify", NotifyEntity=NotifyEntity)
    _module("homeassistant.components.event", EventEntity=EventEntity)
    _module("homeassistant.helpers.entity_platform", async_get_current_platform=lambda: PLATFORM)
    _module("homeassistant.components.select", SelectEntity=SelectEntity)
    _module("homeassistant.helpers.selector", EntitySelector=_Sel, EntitySelectorConfig=_Cfg, SelectSelector=_Sel,
            SelectSelectorConfig=_Cfg, SelectSelectorMode=types.SimpleNamespace(DROPDOWN="dropdown"))
    sys.modules["homeassistant.helpers"].selector = sys.modules["homeassistant.helpers.selector"]
    _module("voluptuous", Schema=Schema, Required=Required, Optional=Optional, In=In,
            All=lambda *a: ("all", a), Coerce=lambda t: ("coerce", t), Range=lambda **kw: ("range", kw))
