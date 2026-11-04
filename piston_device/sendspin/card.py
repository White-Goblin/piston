"""The on-device media card state: what is playing, from the Sendspin metadata and controller roles plus this player's volume.

Pure logic (no I/O). `snapshot()` returns the dict the Steam UI shows; the position is extrapolated here so the frontend only
runs a plain one-second timer from `updated_at`."""
from .messages import CARD_COMMANDS


class CardState:
    def __init__(self):
        self.clear()

    def clear(self) -> None:
        """Back to nothing (the session ended)."""
        self._session = False
        self._meta = {}
        self._progress = None       # (track_progress_ms, track_duration_ms, speed_x1000, timestamp_server_us)
        self._offered = set()
        self._art = None
        self._volume, self._muted = 0, False

    def set_session(self, admitted: bool) -> None:
        self._session = bool(admitted)

    def apply_metadata(self, meta: dict) -> None:
        """A server/state `metadata` object. It is always complete, so it replaces the previous one; omitted progress clears the position."""
        self._meta = {k: meta.get(k) or "" for k in ("title", "artist", "album")}
        progress = meta.get("progress")
        if isinstance(progress, dict) and progress.get("track_progress") is not None:
            self._progress = (progress["track_progress"], progress.get("track_duration") or 0,
                              progress.get("playback_speed") if progress.get("playback_speed") is not None else 1000,
                              meta.get("timestamp"))
        else:
            self._progress = None

    def apply_controller(self, ctrl: dict) -> None:
        self._offered = set(ctrl.get("supported_commands") or ())

    def set_volume(self, volume: int, muted: bool) -> None:
        self._volume, self._muted = int(volume), bool(muted)

    def set_cover(self, art_id) -> None:
        self._art = art_id

    def commands(self) -> list:
        return [c for c in CARD_COMMANDS if c in self._offered]

    def snapshot(self, now_server_us: int | None, wall_now: float) -> dict:
        """The card. `now_server_us` is the current server time (None while the clock is unusable: the position is then the
        reported one); `wall_now` is the device wall-clock in seconds, sent as `updated_at`."""
        title, artist, album = (self._meta.get(k, "") for k in ("title", "artist", "album"))
        playing, position, duration, speed = False, None, None, 0.0
        if self._progress is not None:
            progress, total, speed_x1000, stamp = self._progress
            speed = speed_x1000 / 1000
            playing = speed_x1000 > 0
            duration = total if total > 0 else None
            elapsed_ms = (now_server_us - stamp) / 1000 if now_server_us is not None and stamp is not None else 0
            position = progress + elapsed_ms * speed
            position = max(0.0, position)
            if duration is not None:
                position = min(position, duration)
            position = int(position)
        return {
            "active": bool(self._session and (title or artist)),
            "title": title, "artist": artist, "album": album,
            "art": self._art,
            "playing": playing,
            "position_ms": position, "duration_ms": duration,
            "updated_at": wall_now,
            "speed": speed,
            "commands": self.commands(),
            "volume": self._volume, "muted": self._muted,
        }


def changed(old: dict | None, new: dict) -> bool:
    """Whether a push is needed: everything but the moving clock counts (position and `updated_at` are the frontend's to advance;
    a playing card only needs a new push when something else changed, or when its position was re-based)."""
    if old is None:
        return True
    keys = ("active", "title", "artist", "album", "art", "playing", "duration_ms", "speed", "commands", "volume", "muted")
    if any(old[k] != new[k] for k in keys):
        return True
    if not new["playing"]:
        return old["position_ms"] != new["position_ms"]
    # playing: a position that differs from the frontend's own extrapolation by more than a second is a seek
    expected = (old["position_ms"] or 0) + (new["updated_at"] - old["updated_at"]) * 1000 * new["speed"]
    return new["position_ms"] is not None and abs(new["position_ms"] - expected) > 1000
