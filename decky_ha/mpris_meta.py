"""Parse the text `busctl get-property ... Metadata` and `... Position` print for an MPRIS player (stdlib only, no bus access).

Metadata looks like: a{sv} 6 "mpris:length" x 1841241000 "xesam:artist" as 1 "Name" "xesam:title" s "Title" ...
The art URL can be a data: URL of hundreds of kilobytes, so the scan is a single linear pass and the URL is returned untouched."""
import re

MAX_TITLE_LEN = 255
MAX_TEXT_LEN = 255

# One token: a quoted string (C-style escapes) or a bare word. The quoted form is unrolled so it stays linear on long strings.
_TOKEN = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"|(\S+)', re.S)
_ESCAPE = re.compile(r"\\(x[0-9a-fA-F]{2}|.)", re.S)
_SIMPLE = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_NUMERIC = {"y", "n", "q", "i", "u", "x", "t", "d"}
_TEXT = {"s", "o", "g"}


def _unescape(text: str) -> str:
    if "\\" not in text:
        return text
    chars = []
    pending = bytearray()  # \xNN escapes are UTF-8 bytes

    def flush() -> None:
        if pending:
            chars.append(pending.decode("utf-8", "replace"))
            pending.clear()

    pos = 0
    for match in _ESCAPE.finditer(text):
        piece = text[pos:match.start()]
        if piece:
            flush()
            chars.append(piece)
        pos = match.end()
        code = match.group(1)
        if code.startswith("x") and len(code) == 3:
            pending.append(int(code[1:], 16))
        else:
            flush()
            chars.append(_SIMPLE.get(code, code))
    flush()
    chars.append(text[pos:])
    return "".join(chars)


def _tokens(raw: str):
    for match in _TOKEN.finditer(raw):
        if match.group(1) is not None:
            yield True, match.group(1)
        else:
            yield False, match.group(2)


def _number(word: str):
    try:
        return int(word)
    except ValueError:
        try:
            return float(word)
        except ValueError:
            return None


def _fields(raw: str) -> dict:
    """{key: value} for the dictionary; a value is a str, a number, a bool or a list of str. Stops at a type it cannot skip."""
    out: dict = {}
    toks = _tokens(raw)
    try:
        for quoted, word in toks:
            if not quoted and word == "a{sv}":
                break
        next(toks)  # the entry count
        while True:
            quoted, key = next(toks)
            if not quoted:
                return out
            _, vtype = next(toks)
            if vtype in _TEXT:
                _, value = next(toks)
                out[key] = _unescape(value)
            elif vtype in _NUMERIC:
                _, value = next(toks)
                out[key] = _number(value)
            elif vtype == "b":
                _, value = next(toks)
                out[key] = value == "true"
            elif vtype == "as":
                _, count = next(toks)
                out[key] = [_unescape(next(toks)[1]) for _ in range(int(count))]
            else:
                return out  # a type with no simple layout: later entries cannot be located
    except (StopIteration, ValueError):
        return out


def parse_metadata(raw) -> dict:
    """title, artist, album, length (seconds), trackid, art_url - only the ones the player really gave (non-empty)."""
    if not isinstance(raw, str) or not raw:
        return {}
    fields = _fields(raw)
    result: dict = {}

    def text(key: str, name: str, limit: int = MAX_TEXT_LEN) -> None:
        value = fields.get(key)
        if isinstance(value, list):
            value = ", ".join(v for v in value if v)
        if isinstance(value, str) and value.strip():
            result[name] = value.strip()[:limit]

    text("xesam:title", "title", MAX_TITLE_LEN)
    text("xesam:artist", "artist")
    text("xesam:album", "album")
    text("mpris:trackid", "trackid", 512)
    url = fields.get("mpris:artUrl")
    if isinstance(url, str) and url.strip():
        result["art_url"] = url.strip()
    length = fields.get("mpris:length")
    if isinstance(length, (int, float)) and not isinstance(length, bool) and length > 0:
        result["length"] = length / 1_000_000
    return result


def parse_position(raw) -> float | None:
    """Seconds, from `x 289428972` (microseconds)."""
    if not isinstance(raw, str):
        return None
    parts = raw.split()
    if len(parts) == 2 and parts[0] in ("x", "t", "i", "u"):
        value = _number(parts[1])
        if value is not None and value >= 0:
            return value / 1_000_000
    return None
