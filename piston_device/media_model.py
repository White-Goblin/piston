"""Which MPRIS player describes the running app, and the `media` state group built from it (pure functions, no bus access).

A player is a dict: {"name": bus name, "status": "Playing" | "Paused" | "Stopped" | "", "meta": parse_metadata() result,
"position": seconds | None}. An app is {"appid", "name", "game": bool} or None (nothing running, or the desktop session)."""
import time

STATUS_ORDER = {"Playing": 0, "Paused": 1}
SHORTCUT_APPID_MIN = 2 ** 31  # Steam gives non-Steam shortcuts ids from this value up


def app_info(app_state: dict, offered: list) -> dict | None:
    """The running app as choose_player/build_media want it; None when no app runs."""
    appid = str(app_state.get("appid") or "")
    if not appid:
        return None
    kinds = {str(a.get("appid")): a.get("kind") for a in offered or []}
    if appid in kinds:
        game = kinds[appid] != "shortcut"
    else:
        game = not (appid.isdigit() and int(appid) >= SHORTCUT_APPID_MIN)
    return {"appid": appid, "name": str(app_state.get("name") or ""), "game": game}


def choose_player(players: list, app: dict | None) -> dict | None:
    """A Playing player wins. A Paused one is used only when no game runs, so a forgotten background tab never
    replaces a game. Stopped players and players with no status count as nothing."""
    ranked = sorted((p for p in players if p.get("status") in STATUS_ORDER), key=lambda p: STATUS_ORDER[p["status"]])
    for player in ranked:
        if player["status"] == "Playing" or not (app and app.get("game")):
            return player
    return None


def build_media(player: dict | None, app: dict | None, art_id: str | None = None, now: float | None = None) -> dict:
    """The `media` group: {} when there is nothing to show."""
    app_name = (app or {}).get("name") or ""
    if player is not None:
        meta = player.get("meta") or {}
        media = {"kind": "video", "source": "mpris"}
        title = meta.get("title") or app_name
        if title:
            media["title"] = title
        for key in ("artist", "album"):
            if meta.get(key):
                media[key] = meta[key]
        length, position = meta.get("length"), player.get("position")
        if length and position is not None:
            media["duration"] = round(length, 3)
            media["position"] = round(min(max(position, 0.0), length), 3)
            media["position_at"] = round(time.time() if now is None else now, 3)
        if art_id:
            media["art"] = art_id
        return media if len(media) > 2 else {}  # a player that told us nothing and no app name: nothing to show
    if app_name:
        media = {"title": app_name, "kind": "app", "source": "steam"}
        if art_id:
            media["art"] = art_id
        return media
    return {}
