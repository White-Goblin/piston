"""Self-update on the device: take an `update` request from the integration, download and verify the release zip,
keep a revert copy and hand the verified zip to the Steam UI frontend, which has Decky Loader install it.

The backend cannot replace its own files (the plugin folder belongs to root), so it only prepares the install.
State machine: idle -> downloading -> waiting_for_ui -> installing -> (plugin reload); any state -> failed(reason).
"""
import asyncio
import json
import os
import time

import decky

from . import update_check, update_fetch
from .proto import messages as m
from .settings import write_json_atomic
from .version import plugin_version

ACTIVE_STATES = ("downloading", "waiting_for_ui", "waiting_for_confirmation", "installing")
MARKER_FILE = "update.json"
PENDING_TTL = 24 * 3600   # a verified zip waits this long for the Steam UI (desktop mode has none)
CONFIRM_WINDOW = 30 * 60  # the user has this long to approve Decky Loader's dialog before the update counts as not confirmed
REVERT_WINDOW = 5 * 60    # the new version must have confirmed itself by then
HEALTHY_AFTER = 60        # seconds the new backend runs, listening, before it counts as healthy


class UpdateMixin:
    def _init_update(self) -> None:
        self.update_info = {"state": "idle", "version": "", "reason": None}
        self.update_task = None
        self.update_pending_zip = None   # {version, sha256, path, revert_path, revert_sha256, expires}
        self.update_confirm_task = None  # the new version confirming itself after an install
        self.update_approval_task = None  # waiting for the user to approve Decky Loader's dialog

    # --- paths --------------------------------------------------------------

    def _marker_path(self) -> str:
        return os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, MARKER_FILE)

    def _update_dir(self) -> str:
        return decky.DECKY_PLUGIN_RUNTIME_DIR

    # --- status -------------------------------------------------------------

    def _update_status(self) -> dict:
        info = self.update_info
        status = {"state": info["state"], "version": info["version"]}
        if info["reason"]:
            status["reason"] = info["reason"]
        return status

    def _set_update(self, state: str, version: str = "", reason: str | None = None) -> None:
        self.update_info = {"state": state, "version": version, "reason": reason}
        decky.logger.info("update: %s %s%s", state, version, f" ({reason})" if reason else "")
        self.server.push_update_status(state, version, reason)

    def _resend_update_status(self) -> None:
        """A new connection learns an update that is under way (or failed) without waiting for the next change."""
        if self.update_info["state"] != "idle":
            self.server.push_update_status(**{k: v for k, v in self._update_status().items() if k in ("state", "version", "reason")})

    # --- the request --------------------------------------------------------

    def _on_update_frame(self, frame: dict) -> None:
        """Called by the protocol server (sync, must return quickly). Raises ProtocolError to refuse."""
        if self.update_info["state"] in ACTIVE_STATES:
            raise m.ProtocolError(m.BUSY, "an update is already running")
        try:
            update_check.validate_request(frame["version"], frame["url"], frame["sha256"], plugin_version(decky.DECKY_PLUGIN_DIR))
        except update_check.UpdateRefused as e:
            raise m.ProtocolError(m.BAD_REQUEST, e.message) from None
        self._set_update("downloading", frame["version"])
        self.update_task = self.loop.create_task(self._run_update(frame["version"], frame["url"], frame["sha256"]))

    async def _run_update(self, version: str, url: str, sha256: str) -> None:
        directory = self._update_dir()
        tmp = os.path.join(directory, f"download-{version}.zip")
        pending = os.path.join(directory, f"pending-{version}.zip")
        try:
            os.makedirs(directory, exist_ok=True)
            await asyncio.to_thread(update_fetch.download, url, tmp)
            await asyncio.to_thread(update_fetch.verify_sha256, tmp, sha256)
            await asyncio.to_thread(update_check.validate_archive, tmp, version)
            installed = plugin_version(decky.DECKY_PLUGIN_DIR) or "unknown"
            revert_path = await asyncio.to_thread(update_fetch.make_revert_copy, decky.DECKY_PLUGIN_DIR, directory, installed)
            revert_sha256 = await asyncio.to_thread(update_fetch.sha256_of, revert_path)
            os.replace(tmp, pending)
            self.update_pending_zip = {"version": version, "sha256": sha256, "path": pending, "revert_path": revert_path,
                                       "revert_sha256": revert_sha256, "expires": time.time() + PENDING_TTL}
            write_json_atomic(self._marker_path(), {
                "target": version, "previous": installed, "sha256": sha256, "revert_path": revert_path,
                "revert_sha256": revert_sha256, "state": "waiting_for_ui",
                "expires": time.time() + PENDING_TTL, "deadline": time.time() + PENDING_TTL + REVERT_WINDOW,
                "confirmed": False})
            self._set_update("waiting_for_ui", version)
            await decky.emit("update", version)
        except update_check.UpdateRefused as e:
            self._fail_update(version, e.reason, e.message)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a broken update must never take the plugin down
            decky.logger.exception("update failed")
            self._fail_update(version, "download", str(e))
        finally:
            self._remove_quietly(tmp)

    def _fail_update(self, version: str, reason: str, message: str = "") -> None:
        decky.logger.warning("update to %s failed (%s): %s", version, reason, message)
        self._drop_pending()
        self._set_update("failed", version, reason)

    def _drop_pending(self) -> None:
        """Forget a prepared update: the zip, the marker and the wait for the user's approval."""
        task = self.update_approval_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        pending, self.update_pending_zip = self.update_pending_zip, None
        if pending:
            self._remove_quietly(pending["path"])
        self._remove_quietly(self._marker_path())

    @staticmethod
    def _remove_quietly(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    # --- the one-shot zip route and the callables for the Steam UI frontend -------------------------------------------

    def _pending_update(self):
        """The prepared update, or None (also when it waited longer than PENDING_TTL: then it is dropped, status idle)."""
        pending = self.update_pending_zip
        if pending is not None and time.time() > pending["expires"]:
            decky.logger.info("update %s waited too long for the Steam UI: dropped", pending["version"])
            self._drop_pending()
            self._set_update("idle")
            return None
        return pending

    async def update_pending(self, requested: bool = False) -> dict | None:
        """Frontend callable: what to ask Decky Loader to install now, or None.

        Normally only while the update waits for the Steam interface. With `requested=True` it is also given while the
        dialog is believed to be open: a Steam interface that has just loaded knows that any dialog from before is gone
        (the interface restarted), and the toast's click asks for the dialog again.

        The URLs are `file://` + the absolute path with no encoding (the Loader fails on a percent-encoded path, and the
        data folder name has spaces). `revert_*` describe the saved copy of the version that runs now."""
        pending = self._pending_update()
        wanted = ("waiting_for_ui", "waiting_for_confirmation") if requested else ("waiting_for_ui",)
        if pending is None or self.update_info["state"] not in wanted:
            return None
        return {"version": pending["version"], "sha256": pending["sha256"], "url": "file://" + pending["path"],
                "previous": (self._read_marker() or {}).get("previous", ""), "revert_url": "file://" + pending["revert_path"],
                "revert_sha256": pending["revert_sha256"]}

    async def update_report(self, result: dict) -> None:
        """Frontend callable: {"result": "requested"} once the install request is with Decky Loader (its dialog is on the
        screen and the user has to approve it), or {"result": "error", "detail": str} when the request could not be made."""
        pending = self.update_pending_zip
        if pending is None or self.update_info["state"] not in ("waiting_for_ui", "waiting_for_confirmation"):
            return
        if isinstance(result, dict) and result.get("result") == "requested":  # also a new request for a dialog that was lost
            confirm_by = time.time() + CONFIRM_WINDOW
            marker = self._read_marker() or {}
            marker.update(state="waiting_for_confirmation", confirm_by=confirm_by)
            write_json_atomic(self._marker_path(), marker)
            if self.update_info["state"] != "waiting_for_confirmation":
                self._set_update("waiting_for_confirmation", pending["version"])
            self._start_approval_timer(pending["version"], CONFIRM_WINDOW)  # a new request gives the user a full window again
            return
        detail = str(result.get("detail", "")) if isinstance(result, dict) else ""
        self._fail_update(pending["version"], "installer", detail[:200])

    def _start_approval_timer(self, version: str, seconds: float) -> None:
        if self.update_approval_task is not None:
            self.update_approval_task.cancel()
        self.update_approval_task = self.loop.create_task(self._approval_timeout(version, seconds))

    async def _approval_timeout(self, version: str, seconds: float) -> None:
        """Cancelled and ignored dialogs look the same from here: after the window the update counts as not confirmed."""
        await asyncio.sleep(seconds)
        if self.update_info["state"] == "waiting_for_confirmation" and self.update_info["version"] == version:
            self._fail_update(version, "not_confirmed", "the install was not approved in time")

    # --- start-up: did the last update install? ---------------------------------------------------------------------

    def _read_marker(self) -> dict | None:
        try:
            with open(self._marker_path(), encoding="utf-8") as f:
                marker = json.load(f)
        except (OSError, ValueError):
            return None
        return marker if isinstance(marker, dict) and isinstance(marker.get("target"), str) else None

    async def update_marker(self) -> dict | None:
        """Frontend callable for the revert watchdog: the pending update's marker, or None."""
        return self._read_marker()

    def _update_startup(self) -> None:
        """Run once the protocol server is up. Decides what an update marker left by the previous run means."""
        marker = self._read_marker()
        if marker is None:
            return
        installed = plugin_version(decky.DECKY_PLUGIN_DIR)
        target = marker["target"]
        now = time.time()
        if installed == target:
            # The new version is running. It counts as installed once it has kept running, listening, for a while.
            decky.logger.info("update: running %s, confirming after %s s", target, HEALTHY_AFTER)
            self.update_confirm_task = self.loop.create_task(self._confirm_update(target))
        elif (marker.get("state") == "waiting_for_confirmation" and now >= marker.get("confirm_by", 0)):
            # The dialog has been waiting longer than the window: the user did not approve it.
            decky.logger.warning("update to %s was not approved in time", target)
            self._remove_quietly(self._pending_path(target))
            self._remove_quietly(self._marker_path())
            self.update_info = {"state": "failed", "version": target, "reason": "not_confirmed"}
        elif (marker.get("state") in ("waiting_for_ui", "waiting_for_confirmation") and now < marker.get("expires", 0)
              and os.path.exists(self._pending_path(target))):
            # Restarted (reboot, desktop mode) before the install happened: offer the same verified zip again.
            self.update_pending_zip = {"version": target, "sha256": marker.get("sha256", ""), "path": self._pending_path(target),
                                       "revert_path": marker.get("revert_path", ""), "revert_sha256": marker.get("revert_sha256", ""),
                                       "expires": marker["expires"]}
            self.update_info = {"state": "waiting_for_ui", "version": target, "reason": None}
            self.loop.create_task(decky.emit("update", target))
        else:
            # The install did not happen (or this is the old version again after a revert): say so, clean up.
            decky.logger.warning("update to %s did not take effect (running %s)", target, installed)
            self._remove_quietly(self._pending_path(target))
            self._remove_quietly(self._marker_path())
            self.update_info = {"state": "failed", "version": target, "reason": "installer"}

    def _pending_path(self, version: str) -> str:
        return os.path.join(self._update_dir(), f"pending-{version}.zip")

    async def _confirm_update(self, target: str) -> None:
        await asyncio.sleep(HEALTHY_AFTER)
        if self.server is None or not self.server.port:
            return  # not listening: stays unconfirmed, the frontend watchdog reverts after the deadline
        marker = self._read_marker()
        if marker is not None and marker["target"] == target:
            self._remove_quietly(self._marker_path())
            self._remove_quietly(self._pending_path(target))
            decky.logger.info("update to %s confirmed", target)
