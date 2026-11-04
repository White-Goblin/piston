"""What happens when the device wakes: re-read the real state, wait for the network, then tell Home Assistant.

Replaces re-sending the cached state (which was stale: a game closed or the audio changed during the suspend). The
order matters because the wired network needs 9-14 s to come back after a resume (swe/notes/push-state/baseline.md):

1. drop anything that cannot survive the suspend (picture-in-picture sessions);
2. read the local state (session, audio, sensors, controllers) while waiting for the network to return;
3. restart the mDNS advertisement and re-read the network and Wake on LAN facts once the network is up;
4. send the groups that changed, and `resumed` last, so the first live frame follows fresh data.

Pushes are held during steps 2-3 and sent in step 4. A read that fails or takes too long is skipped, not fatal.
"""
import asyncio

import decky

from .netwait import wait_for_network

RESUME_READ_TIMEOUT = 3.0
APP_SYNC_FIRST_WAIT = 2.0   # the first running-app read happens inside the refresh
APP_SYNC_WINDOW = 10.0      # then the Steam UI gets this long in total (gate 011/004: normally ready at once)
APP_SYNC_RETRY = 1.0


class ResumeMixin:
    async def _announce_resume(self) -> None:
        decky.logger.info("Resumed from suspend")
        if getattr(self, "pip_sessions", None):
            # The stream cannot survive the suspend (the network went away): drop the overlay and the relay session
            self._pip_close_all(notify=True)
            await decky.emit("pip", {"action": "hide"})
        before = dict(self.groups)
        token = self._hold_pushes()
        try:
            ready = False

            async def network() -> None:
                nonlocal ready
                ready = await wait_for_network()

            self._app_sync_ok = False
            await asyncio.gather(network(), *(self._resume_read(name, read) for name, read in self._resume_reads()))
            if not ready:
                decky.logger.warning("The network did not come back within the wait; continuing without the network facts")
            self._restart_advertiser()
            if ready:
                await self._resume_read("device", self._push_device)
        finally:
            self._release_pushes(token, before)  # also when cancelled by another sleep: nothing stays held
        self._push_event("resumed")
        hook = getattr(self, "_sendspin_wake", None)
        if hook is not None:
            self._sendspin_wake_task = asyncio.get_running_loop().create_task(hook())  # in the background: HA is not kept waiting
        if not self._app_sync_ok:
            # The Steam UI was not ready: push what is known (done) and keep asking for a while, in the background
            self._app_task = asyncio.get_running_loop().create_task(self._retry_app_sync())

    def _resume_reads(self) -> list:
        """(name, async callable) for the reads that need no network."""
        return [("session", self._check_session), ("audio", self._resume_audio), ("sensors", self._resume_sensors),
                ("controllers", self._refresh_os_controllers), ("apps", self._resume_apps)]

    async def _request_app_sync(self, wait: float) -> bool:
        """Ask the frontend to report the running app; True when it answered within `wait` seconds."""
        self._app_synced.clear()
        await decky.emit("app_sync", {})
        try:
            await asyncio.wait_for(self._app_synced.wait(), wait)
            return True
        except asyncio.TimeoutError:
            return False

    async def _resume_apps(self) -> None:
        """Re-read what is running (a game may have ended or crashed meanwhile). The frontend reports through
        report_app_state, which lands in the held pushes. Desktop mode has no Steam UI to ask."""
        await self._check_session()
        if self.session == "desktop":
            self._app_sync_ok = True
            return
        self._app_sync_ok = await self._request_app_sync(APP_SYNC_FIRST_WAIT)

    async def _retry_app_sync(self) -> None:
        waited = APP_SYNC_FIRST_WAIT
        while waited < APP_SYNC_WINDOW:
            if await self._request_app_sync(APP_SYNC_RETRY):
                return
            waited += APP_SYNC_RETRY
        decky.logger.warning("Resume refresh: the Steam UI did not report the running app in time")

    async def _resume_audio(self) -> None:
        await self._detect_audio_mode()
        await self._push_audio()

    async def _resume_sensors(self) -> None:
        sensors = await self.loop.run_in_executor(None, self._read_sensors)
        if sensors:
            self.sensor_values = sensors
            self._push("sensors", sensors)

    async def _resume_read(self, name: str, read) -> None:
        try:
            await asyncio.wait_for(read(), RESUME_READ_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            decky.logger.warning(f"Resume refresh: reading {name} timed out")
        except Exception as e:  # noqa: BLE001
            decky.logger.warning(f"Resume refresh: reading {name} failed: {e}")

    def _restart_advertiser(self) -> None:
        """Announce on mDNS again (the multicast membership is gone after a suspend); in the background, it takes ~2 s."""
        advertiser = getattr(self, "advertiser", None)
        if advertiser is None:
            return

        async def restart() -> None:
            try:
                await advertiser.restart()
            except Exception as e:  # noqa: BLE001
                decky.logger.warning(f"mDNS restart failed: {e}")

        self._mdns_task = asyncio.get_running_loop().create_task(restart())
