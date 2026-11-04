"""Validation for self-update: the request from the integration and the downloaded archive (pure, no network).

The zip runs as the device user, so everything is checked before anything is installed (see docs/protocol.md).
"""
import io
import json
import posixpath
import re
import zipfile

from .proto import messages as m

RELEASE_REPO = "White-Goblin/piston"
PLUGIN_DIR = "Piston"
MAX_ZIP_BYTES = 50 * 1024 * 1024          # the download
MAX_UNPACKED_BYTES = 200 * 1024 * 1024    # what the archive claims to hold
REQUIRED_FILES = ("main.py", "plugin.json", "package.json")


class UpdateRefused(Exception):
    """`reason` is one of the update_status reasons (bad_request, archive, ...)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason, self.message = reason, message


def asset_url(version: str, repo: str = RELEASE_REPO) -> str:
    return f"https://github.com/{repo}/releases/download/v{version}/piston-{version}.zip"


def version_key(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))


def validate_request(version, url, sha256, installed: str | None, repo: str = RELEASE_REPO) -> None:
    """Raise UpdateRefused('bad_request') unless the request is well formed, from the release repo and not a no-op."""
    if not isinstance(version, str) or not m.VERSION_RE.match(version):
        raise UpdateRefused("bad_request", "the version must look like 1.2.3")
    if not isinstance(sha256, str) or not m.SHA256_RE.match(sha256):
        raise UpdateRefused("bad_request", "the checksum must be 64 lowercase hex digits")
    if url != asset_url(version, repo):
        raise UpdateRefused("bad_request", "the address is not the release asset of this version")
    if version == installed:
        raise UpdateRefused("bad_request", "that version is already installed")


def validate_archive(data, version: str) -> None:
    """Raise UpdateRefused('archive') unless `data` (bytes or a path) is a plugin zip of `version` with a safe layout."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)
    except (zipfile.BadZipFile, OSError):
        raise UpdateRefused("archive", "not a zip file") from None
    with zf:
        total = 0
        names = set()
        for info in zf.infolist():
            name = info.filename
            if (name.startswith("/") or "\\" in name or ".." in name.split("/") or posixpath.isabs(name)
                    or re.match(r"^[A-Za-z]:", name)):
                raise UpdateRefused("archive", f"unsafe path {name!r}")
            if name.split("/")[0] != PLUGIN_DIR:
                raise UpdateRefused("archive", f"{name!r} is outside the {PLUGIN_DIR!r} folder")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateRefused("archive", f"{name!r} is a symbolic link")
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise UpdateRefused("archive", "the archive is too large when unpacked")
            names.add(name)
        for required in REQUIRED_FILES:
            if f"{PLUGIN_DIR}/{required}" not in names:
                raise UpdateRefused("archive", f"{required} is missing")
        try:
            package = json.loads(zf.read(f"{PLUGIN_DIR}/package.json"))
        except (ValueError, KeyError):
            raise UpdateRefused("archive", "package.json is unreadable") from None
        if not isinstance(package, dict) or package.get("version") != version:
            raise UpdateRefused("archive", "the archive holds a different version")
