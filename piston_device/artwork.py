"""Artwork for the media group: load an image from an MPRIS art URL or from Steam's local art, keep the current one
and serve it in chunks. Blocking functions (run them in an executor); stdlib only, no image resizing is possible
on the device, so an image is passed on as it is or not at all."""
import base64
import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import decky

from .proto import messages as m

MAX_BYTES = m.MAX_ARTWORK_BYTES
FETCH_TIMEOUT = 5.0
FETCH_TOTAL_SECONDS = 15.0  # a server that drips bytes cannot hold the loader longer than this
READ_BLOCK = 64 * 1024
GRID_NAMES = ("{id}p.png", "{id}p.jpg", "{id}.png", "{id}.jpg", "{id}_hero.png", "{id}_hero.jpg")
LIBRARY_NAMES = ("library_600x900.jpg", "header.jpg")


class Art:
    __slots__ = ("id", "content_type", "data")

    def __init__(self, id: str, content_type: str, data: bytes):
        self.id, self.content_type, self.data = id, content_type, data


def sniff(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def make_art(data: bytes | None, why: str = "") -> Art | None:
    """An Art from raw bytes, or None (with a debug log) for no data, a non-image or an image over the cap."""
    if not data:
        return None
    if len(data) > MAX_BYTES:
        decky.logger.debug(f"Artwork dropped: {len(data)} bytes is over the {MAX_BYTES} byte cap ({why})")
        return None
    content_type = sniff(data)
    if content_type is None:
        decky.logger.debug(f"Artwork dropped: not a known image type ({why})")
        return None
    return Art(hashlib.sha256(data).hexdigest()[:16], content_type, data)


# --- URLs ---------------------------------------------------------------------

def _from_data_url(url: str) -> bytes | None:
    head, _, payload = url[5:].partition(",")
    if len(payload) > MAX_BYTES * 4 // 3 + 16:
        return None
    if head.endswith(";base64"):
        try:
            return base64.b64decode(urllib.parse.unquote(payload) if "%" in payload else payload, validate=False)
        except ValueError:
            return None
    return urllib.parse.unquote_to_bytes(payload)


def _from_file_url(url: str) -> bytes | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc not in ("", "localhost"):
        return None
    path = urllib.parse.unquote(parsed.path)
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read(MAX_BYTES + 1)


class _OnlyWebRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme not in ("http", "https"):
            raise urllib.error.URLError(f"redirect to {urllib.parse.urlparse(newurl).scheme or 'nothing'}: refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _from_web_url(url: str, timeout: float) -> bytes | None:
    opener = urllib.request.build_opener(_OnlyWebRedirects)
    deadline = time.monotonic() + max(FETCH_TOTAL_SECONDS, timeout)
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "piston"}), timeout=timeout) as response:
        chunks, total = [], 0
        while total <= MAX_BYTES:
            if time.monotonic() > deadline:
                return None
            block = response.read(READ_BLOCK)
            if not block:
                break
            chunks.append(block)
            total += len(block)
        return b"".join(chunks)


def load_from_url(url: str, timeout: float = FETCH_TIMEOUT) -> Art | None:
    """An MPRIS art URL (data:, file:, http:, https:) as an image, or None when it cannot be used."""
    if not isinstance(url, str):
        return None
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    try:
        if scheme == "data":
            data = _from_data_url(url)
        elif scheme == "file":
            data = _from_file_url(url)
        elif scheme in ("http", "https"):
            data = _from_web_url(url, timeout)
        else:
            decky.logger.debug(f"Artwork skipped: unsupported URL scheme {scheme or '(none)'}")
            return None
    except (OSError, ValueError, urllib.error.URLError) as e:
        if isinstance(e, urllib.error.HTTPError):
            e.close()
        decky.logger.debug(f"Artwork could not be loaded from a {scheme}: URL: {e}")
        return None
    return make_art(data, f"{scheme}: URL")


# --- Steam's own art ----------------------------------------------------------

def steam_art_paths(appid: str, steam_root: str) -> list:
    """Candidate files in preference order (portrait before wide): the user's custom grid art, then the library cache."""
    appid = str(appid)
    if not appid.isdigit():
        return []
    paths = []
    userdata = os.path.join(steam_root, "userdata")
    try:
        users = sorted(os.listdir(userdata))
    except OSError:
        users = []
    for user in users:
        for pattern in GRID_NAMES:
            paths.append(os.path.join(userdata, user, "config", "grid", pattern.format(id=appid)))
    cache = os.path.join(steam_root, "appcache", "librarycache", appid)
    paths.extend(os.path.join(cache, name) for name in LIBRARY_NAMES)
    try:
        for entry in sorted(os.listdir(cache)):
            paths.append(os.path.join(cache, entry, "library_header.jpg"))
    except OSError:
        pass
    return paths


def load_steam(appid: str, steam_root: str) -> Art | None:
    for path in steam_art_paths(appid, steam_root):
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                art = make_art(f.read(MAX_BYTES + 1), os.path.basename(path))
        except OSError:
            continue
        if art is not None:
            return art
    return None


# --- the current image --------------------------------------------------------

class ArtStore:
    """Holds the one current image and hands it out in chunks."""

    def __init__(self):
        self.art: Art | None = None

    def set(self, art: Art | None) -> None:
        self.art = art

    @property
    def id(self) -> str | None:
        return self.art.id if self.art else None

    def chunk(self, art_id: str, offset: int = 0) -> tuple:
        """(content_type, total size, bytes, done). LookupError: no such image (any more); ValueError: offset past the end."""
        art = self.art
        if art is None or art.id != art_id:
            raise LookupError(art_id)
        if offset < 0 or offset >= len(art.data):
            raise ValueError(offset)
        piece = art.data[offset:offset + m.ARTWORK_CHUNK_BYTES]
        return art.content_type, len(art.data), piece, offset + len(piece) >= len(art.data)
