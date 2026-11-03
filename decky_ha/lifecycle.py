"""Sleep and shutdown announcements from systemd-logind, for every way the device can go down.

A hardware power button, the idle timer, Home Assistant and the Steam menu all end in a logind suspend, but only
the plugin's own `power` command ever reached the plugin, so `going_to_sleep` was missing for the others. Logind
announces them all with `PrepareForSleep` / `PrepareForShutdown` (measured on the device, see
swe/notes/push-state/gate-results.md). Decky has no runtime pip and `dbus-monitor` is denied to non-root users,
so this uses two system tools as subprocesses:

* `gdbus monitor` for the signals (plain match rules, no monitor privilege);
* `systemd-inhibit --mode=delay ... cat` as a delay inhibitor: while it runs, logind waits (at most its
  InhibitDelayMaxUSec, 5 s here) after announcing the suspend, which is the time `on_sleep` needs to get the
  event out. It is released as soon as `on_sleep` returns, or after `inhibit_cap` seconds, whichever is first.

Any internal error releases the inhibitor at once: this module must never hold up a suspend.
"""
import asyncio
import re
import time
from typing import Awaitable, Callable

import decky

MONITOR_ARGV = ("gdbus", "monitor", "--system", "--dest", "org.freedesktop.login1", "--object-path", "/org/freedesktop/login1")
INHIBIT_ARGV = ("systemd-inhibit", "--what=sleep:shutdown", "--mode=delay", "--who=Decky Home Assistant",
                "--why=Tell Home Assistant before the device goes down", "cat")
SIGNAL = re.compile(r"Manager\.(PrepareForSleep|PrepareForShutdown) \((true|false)")
INHIBIT_CAP_SECONDS = 2.0
RESTART_DELAYS = (1, 2, 5, 10, 30)  # bounded: after these the module stays inactive
STABLE_SECONDS = 60                 # a monitor that ran this long resets the retry count
REASON_TIMEOUT = 1.0

Spawn = Callable[..., Awaitable]


async def shutdown_reason() -> str:
    """"restart" when a reboot is what logind is doing, otherwise "shutdown" (the signal itself carries no reason)."""
    try:
        proc = await asyncio.create_subprocess_exec("systemctl", "list-jobs", "--no-legend",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), REASON_TIMEOUT)
        return "restart" if b"reboot.target" in out else "shutdown"
    except Exception:  # noqa: BLE001
        return "shutdown"


class Lifecycle:
    """`on_sleep()` and `on_shutdown(reason)` are async and should send the event and flush it; `on_resume()` is async too.
    `active` is True while the signal monitor is running."""

    def __init__(self, *, on_sleep: Callable[[], Awaitable], on_shutdown: Callable[[str], Awaitable],
                 on_resume: Callable[[], Awaitable], spawn: Spawn = asyncio.create_subprocess_exec,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, clock: Callable[[], float] = time.monotonic,
                 reason: Callable[[], Awaitable[str]] = shutdown_reason, inhibit_cap: float = INHIBIT_CAP_SECONDS,
                 restart_delays: tuple = RESTART_DELAYS):
        self._on_sleep, self._on_shutdown, self._on_resume = on_sleep, on_shutdown, on_resume
        self._spawn, self._sleep, self._clock, self._reason = spawn, sleep, clock, reason
        self._cap, self._delays = inhibit_cap, restart_delays
        self.active = False
        self._monitor = None
        self._inhibitor = None
        self._task: asyncio.Task | None = None
        self._resume_task: asyncio.Task | None = None
        self._stopped = False

    @property
    def protecting(self) -> bool:
        """True when a sleep announcement is guaranteed to get out before a suspend: the monitor runs and the inhibitor is held."""
        return self.active and self._inhibitor is not None

    async def start(self) -> None:
        self._stopped = False
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        self.active = False
        self._cancel_resume()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._kill(self._monitor)
        self._monitor = None
        await self._release_inhibitor()

    # --- inhibitor -------------------------------------------------------------

    async def _take_inhibitor(self) -> None:
        if self._inhibitor is not None:
            return
        try:
            self._inhibitor = await self._spawn(*INHIBIT_ARGV, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        except OSError as e:
            decky.logger.warning(f"Could not take a sleep inhibitor ({e}); the sleep announcement may race the suspend")

    async def _release_inhibitor(self) -> None:
        proc, self._inhibitor = self._inhibitor, None
        if proc is None:
            return
        try:
            proc.stdin.close()  # `cat` sees EOF and exits, which drops the lock
            await asyncio.wait_for(proc.wait(), 1.0)
        except Exception:  # noqa: BLE001
            await self._kill(proc)

    @staticmethod
    async def _kill(proc) -> None:
        if proc is None:
            return
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), 1.0)
        except Exception:  # noqa: BLE001
            pass

    # --- monitor ---------------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        warned = False
        while not self._stopped:
            started = self._clock()
            try:
                self._monitor = await self._spawn(*MONITOR_ARGV, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.DEVNULL)
            except OSError as e:
                if not warned:
                    decky.logger.warning(f"Sleep announcements unavailable, cannot start gdbus: {e}")
                return  # a missing tool will not appear by itself: stay inactive
            self.active = True
            await self._take_inhibitor()  # held only while the monitor is there to release it
            try:
                await self._read(self._monitor)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                decky.logger.warning(f"Sleep announcement monitor failed: {e}")
            self.active = False
            await self._release_inhibitor()
            await self._kill(self._monitor)
            self._monitor = None
            if self._stopped:
                return
            if self._clock() - started >= STABLE_SECONDS:
                attempt = 0
            if attempt >= len(self._delays):
                decky.logger.warning("Sleep announcements stopped: the signal monitor keeps exiting")
                return
            if not warned:
                decky.logger.warning("The sleep signal monitor exited; restarting it")
                warned = True
            await self._sleep(self._delays[attempt])
            attempt += 1

    async def _read(self, proc) -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                return  # EOF: the monitor exited
            match = SIGNAL.search(line.decode(errors="replace"))
            if match:
                await self._handle(match.group(1), match.group(2) == "true")

    async def _handle(self, signal: str, going_down: bool) -> None:
        if signal == "PrepareForSleep" and not going_down:
            await self._take_inhibitor()  # first: the refresh below can take a while and the device may sleep again
            self._cancel_resume()
            self._resume_task = asyncio.get_running_loop().create_task(self._resume())
            return
        if not going_down:
            return  # PrepareForShutdown(false): a shutdown was cancelled, nothing to say
        self._cancel_resume()  # going down again: whatever the refresh was waiting for is moot
        try:
            if signal == "PrepareForSleep":
                await self._announce(self._on_sleep())
            else:
                await self._announce(self._on_shutdown(await self._reason()))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            decky.logger.warning(f"Handling {signal} failed: {e}")
            await self._release_inhibitor()

    async def _resume(self) -> None:
        """Runs apart from the signal loop: a slow refresh (it may wait for the network) must not block the next signal."""
        try:
            await self._on_resume()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            decky.logger.warning(f"Handling the resume failed: {e}")

    def _cancel_resume(self) -> None:
        if self._resume_task is not None and not self._resume_task.done():
            self._resume_task.cancel()
        self._resume_task = None

    async def _announce(self, coro: Awaitable) -> None:
        """Run the announcement under the inhibitor, then release it: when the callback returns, on error, or at the cap."""
        try:
            await asyncio.wait_for(coro, self._cap)
        except asyncio.TimeoutError:
            decky.logger.warning("The sleep announcement took too long; letting the device go down")
        finally:
            await self._release_inhibitor()
