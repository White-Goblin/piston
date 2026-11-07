"""Device settings: identity, pairing secret and the apps offered as sources."""
import json
import os
import re
import secrets
import socket
import tempfile

import decky

APPID_RE = re.compile(r"^\d{1,12}$")
SENDSPIN_KEY_RE = re.compile(r"[0-9a-f]{64}")
MAX_APPS = 25
# Steam games, and non-Steam shortcuts added to the Steam library
APP_KINDS = ("steam", "shortcut")
SENSOR_INTERVALS = (10, 30, 60, 300)
# Never returned to the panel
SECRET_KEYS = ("secret", "sendspin_private_key")
SENDSPIN_NAME_BYTES = 64  # the Sendspin hello limit
SENDSPIN_NAME_SUFFIX = " audio"  # the default name is "<device name> audio", so it differs from the device's own media player
SENDSPIN_DEFAULT_VOLUME = 50
SENDSPIN_MAX_OUTPUT_DELAY = 5000


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
        "remote_input": True,  # Home Assistant may move the pointer and type (the remote card)
        # Sendspin background player
        "sendspin_enabled": True,
        "sendspin_name": "",  # empty = "<device name> audio"
        "sendspin_private_key": "",  # hex Curve25519 private key, created on first use; its public key is the client id
        "sendspin_volume": SENDSPIN_DEFAULT_VOLUME,
        "sendspin_muted": False,
        "sendspin_output_delay_ms": 0,
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


def _truncate_bytes(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes, cut on a character boundary."""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _int_in(value, low: int, high: int, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high else default


def clean_sendspin(settings: dict) -> dict:
    """Bring the Sendspin keys to valid values (bad stored values fall back to the defaults)."""
    defaults = default_settings()
    out = dict(settings)
    out["sendspin_enabled"] = out["sendspin_enabled"] if isinstance(out.get("sendspin_enabled"), bool) else True
    name = out.get("sendspin_name")
    out["sendspin_name"] = _truncate_bytes(name.strip(), SENDSPIN_NAME_BYTES).strip() if isinstance(name, str) else ""
    key = out.get("sendspin_private_key")
    out["sendspin_private_key"] = key if isinstance(key, str) and SENDSPIN_KEY_RE.fullmatch(key) else ""
    out["sendspin_volume"] = _int_in(out.get("sendspin_volume"), 0, 100, defaults["sendspin_volume"])
    out["sendspin_muted"] = out["sendspin_muted"] if isinstance(out.get("sendspin_muted"), bool) else False
    out["sendspin_output_delay_ms"] = _int_in(out.get("sendspin_output_delay_ms"), 0, SENDSPIN_MAX_OUTPUT_DELAY, 0)
    return out


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
        settings = clean_sendspin({k: stored.get(k, v) for k, v in defaults.items()})
        if not isinstance(settings["remote_input"], bool):
            settings["remote_input"] = True
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

    # --- Sendspin ---------------------------------------------------------------

    def _sendspin_name(self) -> str:
        """The name shown in Music Assistant: the chosen one, else "<device name> audio" (the device name is cut so the
        whole name fits the 64 bytes Sendspin allows)."""
        s = self._load_settings()
        if s["sendspin_name"]:
            return s["sendspin_name"]
        room = SENDSPIN_NAME_BYTES - len(SENDSPIN_NAME_SUFFIX.encode())
        return _truncate_bytes(s["device_name"], room).rstrip() + SENDSPIN_NAME_SUFFIX

    def _sendspin_private_key(self) -> bytes:
        """The device's Sendspin identity key; created and saved on first use (the client id is its public key)."""
        s = self._load_settings()
        if not s["sendspin_private_key"]:
            s = self._update_settings({"sendspin_private_key": secrets.token_hex(32)})
        return bytes.fromhex(s["sendspin_private_key"])

    def _save_sendspin(self, **patch) -> dict:
        """Update Sendspin keys (invalid values raise ValueError; written only when something changed); returns the settings."""
        unknown = set(patch) - {"sendspin_enabled", "sendspin_name", "sendspin_volume", "sendspin_muted", "sendspin_output_delay_ms"}
        if unknown:
            raise KeyError(f"not a Sendspin setting: {', '.join(sorted(unknown))}")
        current = self._load_settings()
        cleaned = clean_sendspin({**current, **patch})
        for key, value in patch.items():
            if key != "sendspin_name" and (cleaned[key] != value or type(cleaned[key]) is not type(value)):  # the name is normalized (trimmed, cut), the rest must be valid
                raise ValueError(f"invalid value for {key}: {value!r}")
        changed = {k: cleaned[k] for k in patch if cleaned[k] != current[k]}
        return self._update_settings(changed) if changed else current

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
