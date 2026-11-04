"""Download a release zip with limits, check its SHA-256 and keep a revert copy of the installed plugin (stdlib only)."""
import hashlib
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from .update_check import MAX_ZIP_BYTES, PLUGIN_DIR, UpdateRefused

DOWNLOAD_TIMEOUT = 60
CHUNK = 64 * 1024
# Releases are served from github.com and redirected to GitHub's own asset hosts.
ALLOWED_HOSTS = ("github.com",)
ALLOWED_SUFFIXES = (".githubusercontent.com",)
# Decky Loader's own Python does not know where the system's certificates are (the download failed with
# CERTIFICATE_VERIFY_FAILED on a real device), so the system bundle is loaded explicitly. First one that loads wins.
CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem", "/etc/ca-certificates/extracted/tls-ca-bundle.pem",
              "/etc/pki/tls/certs/ca-bundle.crt")
# What ships (mirrors scripts/package_plugin.py; a test keeps the two in step)
TOP_FILES = ("main.py", "plugin.json", "package.json", "LICENSE")
TREES = ("dist", "decky_ha", "py_modules")
SKIP_PARTS = {"__pycache__"}
SKIP_SUFFIXES = (".map", ".pyc")


def host_allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES)) and "@" not in parts.netloc


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_allowed(newurl):
            raise urllib.error.URLError(f"redirect to {newurl!r} refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def make_ssl_context(bundles=CA_BUNDLES) -> ssl.SSLContext:
    """The default verifying context plus the first system certificate bundle that can be read."""
    context = ssl.create_default_context()
    for path in bundles:
        try:
            context.load_verify_locations(cafile=path)
            break
        except (OSError, ssl.SSLError):
            continue
    return context


def default_opener(url: str, timeout: float):
    opener = urllib.request.build_opener(_Redirects, urllib.request.HTTPSHandler(context=make_ssl_context()))
    return opener.open(urllib.request.Request(url, headers={"User-Agent": "decky-home-assistant"}), timeout=timeout)


def download(url: str, dest: str, *, opener=default_opener, timeout: float = DOWNLOAD_TIMEOUT, cap: int = MAX_ZIP_BYTES,
             clock=time.monotonic) -> int:
    """Blocking. Save `url` to `dest`; returns the size. Raises UpdateRefused('download') on any problem. Run it in a thread."""
    if not host_allowed(url):
        raise UpdateRefused("download", "the address is not on GitHub")
    deadline = clock() + timeout
    written = 0
    try:
        with opener(url, timeout) as response, open(dest, "wb") as out:
            length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
            if length and length.isdigit() and int(length) > cap:
                raise UpdateRefused("download", "the file is too large")
            while True:
                if clock() > deadline:
                    raise UpdateRefused("download", "the download took too long")
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > cap:
                    raise UpdateRefused("download", "the file is too large")
                out.write(chunk)
    except UpdateRefused:
        _remove(dest)
        raise
    except (OSError, ValueError) as e:  # URLError, timeouts, disk full
        _remove(dest)
        raise UpdateRefused("download", f"download failed: {e}") from None
    return written


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: str, expected: str) -> None:
    if sha256_of(path) != expected:
        raise UpdateRefused("hash", "the download does not match its checksum")


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def shipped_files(plugin_dir: str) -> list:
    """(absolute path, path inside the plugin folder) for what the installed plugin consists of."""
    files = [(os.path.join(plugin_dir, f), f) for f in TOP_FILES if os.path.isfile(os.path.join(plugin_dir, f))]
    for tree in TREES:
        for folder, dirs, names in os.walk(os.path.join(plugin_dir, tree)):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_PARTS)
            for name in sorted(names):
                if not name.endswith(SKIP_SUFFIXES):
                    path = os.path.join(folder, name)
                    files.append((path, os.path.relpath(path, plugin_dir)))
    return files


def make_revert_copy(plugin_dir: str, data_dir: str, version: str) -> str:
    """Zip the installed plugin as `previous-<version>.zip` in `data_dir` (same layout as a release); older copies are removed."""
    target = os.path.join(data_dir, f"previous-{version}.zip")
    tmp = target + ".tmp"
    try:
        os.makedirs(data_dir, exist_ok=True)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for path, name in shipped_files(plugin_dir):
                z.write(path, f"{PLUGIN_DIR}/{name}")
        os.replace(tmp, target)
    except OSError as e:
        _remove(tmp)
        raise UpdateRefused("download", f"could not save the revert copy: {e}") from None
    for name in os.listdir(data_dir):
        if name.startswith("previous-") and name.endswith(".zip") and name != os.path.basename(target):
            _remove(os.path.join(data_dir, name))
    return target
