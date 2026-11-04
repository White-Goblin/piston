"""The installed plugin's version, read from the `package.json` that ships next to `main.py`."""
import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def plugin_version(root: str = _ROOT) -> str | None:
    """`N.N.N` or None when the file is missing or unreadable (then the device does not advertise the update lane)."""
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
            version = json.load(f)["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    from .proto import messages as m
    return version if isinstance(version, str) and m.VERSION_RE.match(version) else None
