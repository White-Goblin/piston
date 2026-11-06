"""Constants for the Piston integration."""

DOMAIN = "piston"
PLATFORMS: list[str] = ["binary_sensor", "button", "event", "media_player", "notify", "select", "sensor", "switch", "update"]

DEFAULT_PORT = 47830
# Websocket ping interval. A ping unanswered after half of it closes the socket, so a connection left half-open by a
# suspend is noticed within about 1.5 x this (22 s). The device pings every 20 s and drops a silent peer after 60 s,
# so these do not interact: its own pings are what keep the device side alive.
HEARTBEAT_SECONDS = 15
SERVICE_TYPE = "_piston._tcp.local."

# Plugin self-update (see docs/protocol.md): the integration's version is the version the plugin should run.
RELEASE_REPO = "White-Goblin/piston"
FIRST_LANE_VERSION = "0.4.0"  # the first plugin release that can be updated by the integration

CONF_DEVICE_ID = "device_id"
CONF_SECRET = "secret"  # hex
CONF_HOST = "host"
CONF_PORT = "port"
CONF_MAC = "mac"
CONF_NAME = "name"

OPT_TURN_OFF = "turn_off"  # "sleep" | "shutdown"
OPT_SENSOR_INTERVAL = "sensor_interval"
TURN_OFF_MODES = ("sleep", "shutdown")
SENSOR_INTERVALS = (10, 30, 60, 300)
OPT_PIP_CAMERAS = "pip_cameras"
OPT_PIP_POSITION = "pip_position"
OPT_PIP_SIZE = "pip_size"
OPT_PIP_DURATION = "pip_duration"  # seconds, 0 = until hidden
OPT_PIP_MUTE = "pip_mute"
OPT_ALLOW_ALL_USERS = "allow_all_users"  # remote input (the card) for every user, not only administrators
PIP_POSITIONS = ("top", "top-right", "right", "bottom-right", "bottom", "bottom-left", "left", "top-left")
# Size is the width of a 16:9 box as a fraction of the screen width
PIP_SIZES = {"small": 0.25, "medium": 0.4, "large": 0.55, "extra_large": 0.7}
PIP_DURATIONS = (10, 20, 30, 60, 300, 0)
DEFAULT_OPTIONS = {
    OPT_TURN_OFF: "sleep", OPT_SENSOR_INTERVAL: 10, OPT_PIP_CAMERAS: [], OPT_PIP_POSITION: "top-right",
    OPT_PIP_SIZE: "medium", OPT_PIP_DURATION: 30, OPT_PIP_MUTE: True, OPT_ALLOW_ALL_USERS: False,
}
