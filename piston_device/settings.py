"""Device settings: identity, pairing secret and the apps offered as sources."""
import json
import os
import re
import secrets
import socket
import tempfile

import decky

APPID_RE = re.compile(r"^\d{1,12}$")
MAX_APPS = 25
# Steam games, and non-Steam shortcuts added to the Steam library
APP_KINDS = ("steam", "shortcut")
SENSOR_INTERVALS = (10, 30, 60, 300)
# Never returned to the panel
SECRET_KEYS = ("secret",)


def write_json_atomic(path: str, data: dict) -> None:
    """Replace `path` with `data` so a crash leaves the old or the new file, never a partial one.

    The file holds the pairing secret, so it is created with mode 600."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def default_settings() -> dict:
    return {
        "device_id": "",
        "device_name": socket.gethostname() or "Decky device",
        "secret": "",  # hex; empty while unpaired
        "apps": [],
        "controllers_seen": [],
        "sensor_interval": 10,
    }


def clean_apps(value) -> list:
    """Validate the exposed app list: at most MAX_APPS, unique numeric ids, known kinds."""
    apps = {}
    for app in (value if isinstance(value, list) else [])[:MAX_APPS]:
        appid = str(app.get("appid", "")) if isinstance(app, dict) else ""
        if APPID_RE.match(appid):
            kind = app.get("kind") if app.get("kind") in APP_KINDS else "steam"
            apps[appid] = {"appid": appid, "name": str(app.get("name") or appid)[:80], "kind": kind}
    return list(apps.values())


class SettingsMixin:
    def _settings_path(self) -> str:
        return os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")

    def _read_stored(self) -> dict:
        try:
            with open(self._settings_path()) as f:
                stored = json.load(f)
            return stored if isinstance(stored, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _load_settings(self) -> dict:
        """Current settings. Unknown keys are ignored and dropped
        on the next write; the device id is created and saved on first use."""
        defaults = default_settings()
        stored = self._read_stored()
        settings = {k: stored.get(k, v) for k, v in defaults.items()}
        if not re.fullmatch(r"[a-z0-9]{12}", str(settings["device_id"])):
            settings["device_id"] = secrets.token_hex(6)
            write_json_atomic(self._settings_path(), settings)
        elif set(stored) - set(defaults):
            write_json_atomic(self._settings_path(), settings)
        return settings

    def _update_settings(self, patch: dict) -> dict:
        settings = {**self._load_settings(), **patch}
        write_json_atomic(self._settings_path(), settings)
        return settings

    def _is_paired(self) -> bool:
        return bool(self._load_settings()["secret"])

    async def get_settings(self) -> dict:
        s = self._load_settings()
        return {**{k: v for k, v in s.items() if k not in SECRET_KEYS}, "paired": bool(s["secret"])}

    async def save_apps(self, apps: list) -> list:
        """Panel: replace the list of apps offered as media sources."""
        cleaned = clean_apps(apps)
        self._update_settings({"apps": cleaned})
        push = getattr(self, "_push_apps", None)
        if push is not None:
            push()  # the integration's source list follows immediately
        return cleaned
