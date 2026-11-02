"""Sensor descriptions shared by the plugin and the integration (stdlib only).

Each key is a field of the `sensors` state group. Values are (name, Home Assistant attributes);
`battery_<name>` keys are dynamic and described by `battery_description`.
"""

SENSOR_ENTITIES = {
    "cpu_temp": ("CPU temperature", {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"}),
    "gpu_temp": ("GPU temperature", {"device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement"}),
    "fan_rpm": ("Fan speed", {"unit_of_measurement": "RPM", "state_class": "measurement", "icon": "mdi:fan"}),
    "cpu_load": ("CPU load", {"unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:cpu-64-bit"}),
    "gpu_load": ("GPU load", {"unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:expansion-card"}),
    "mem_used": ("Memory used", {"unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:memory"}),
    "disk_used": ("Disk used", {"unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:harddisk"}),
    "uptime": ("Uptime", {"device_class": "duration", "unit_of_measurement": "s", "state_class": "total_increasing"}),
    "ip": ("IP address", {"icon": "mdi:ip-network"}),
    "wifi_signal": ("Wi-Fi signal", {"device_class": "signal_strength", "unit_of_measurement": "dBm", "state_class": "measurement"}),
}


def battery_description(key: str) -> tuple:
    """(name, attributes) for a dynamic `battery_<name>` key."""
    return ("Battery " + key[8:].replace("_", " "),
            {"device_class": "battery", "unit_of_measurement": "%", "state_class": "measurement"})


def describe(key: str):
    """(name, attributes) for any sensors-group key, or None for an unknown one."""
    if key.startswith("battery_"):
        return battery_description(key)
    return SENSOR_ENTITIES.get(key)
