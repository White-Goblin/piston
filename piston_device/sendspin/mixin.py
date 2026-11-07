"""The Sendspin player inside the plugin backend: owns the listener, the mDNS record and the `sendspin` state group.

`_sendspin_start` / `_sendspin_stop` are idempotent and are what the settings
(enable switch), the plugin start/unload and the sleep/resume handling call."""
import asyncio
import time

import decky

from ..advertise import sendspin_advertiser
from ..proto import messages as m
from . import codecs, crypto
from .listener import DEFAULT_PORT as LISTEN_PORT, SendspinListener
from .messages import CARD_COMMANDS, client_id_for
from .session import SendspinSession
from .sink import PipeWireSink

SLEEP_STOP_TIMEOUT_S = 2.5  # logind gives a delay inhibitor only a few seconds: leaving cleanly must fit in that
SETTINGS_DEBOUNCE_S = 2.0  # Music Assistant raises the volume for an announcement and restores it: only the settled value is saved
CARD_COMMAND_MIN_INTERVAL_S = 0.4  # card buttons: a faster press is ignored (each pause/play makes Music Assistant restart the stream)
SETTING_KEYS = {"volume": "sendspin_volume", "muted": "sendspin_muted", "output_delay_ms": "sendspin_output_delay_ms"}


class SendspinMixin:
    def _init_sendspin(self) -> None:
        self.sendspin_listener = None
        self.sendspin_advertiser = None
        self.sendspin_crypto_error = ""
        self._sendspin_client_id = None
        self._sendspin_pending = {}
        self._sendspin_save_handle = None
        self._sendspin_lock = asyncio.Lock()
        self._sendspin_tasks = set()  # emits in flight
        self._card_command_at = -1e9
        self._sendspin_stopping = (None, None)
        self.sendspin_asleep = False  # between the sleep announcement and the resume refresh the player stays down

    # --- state group -----------------------------------------------------------------------------------------------------------

    def _sendspin_group(self) -> dict:
        settings = self._load_settings()
        listener = self.sendspin_listener
        group = {"enabled": settings["sendspin_enabled"], "status": "waiting", "name": self._sendspin_name()}
        if not settings["sendspin_enabled"]:
            group["status"] = "disabled"
        elif self.sendspin_crypto_error:
            group.update(status="error", error=self.sendspin_crypto_error)
        elif listener is not None and listener.status in ("waiting", "connected", "streaming", "error"):
            group["status"] = listener.status
            if listener.port:
                group["port"] = listener.port
            if listener.server_name:
                group["server_name"] = listener.server_name
            if listener.error:
                group["error"] = listener.error[:m.MAX_SENDSPIN_TEXT_LEN]
        return m.sendspin_group(group)

    def _push_sendspin(self) -> None:
        """Push the group when it changed."""
        try:
            group = self._sendspin_group()
        except Exception as e:  # noqa: BLE001 - a status problem must not break the plugin
            decky.logger.warning(f"Sendspin status failed: {e!r}")
            return
        if self.groups.get(m.SENDSPIN_GROUP) != group:
            self._push(m.SENDSPIN_GROUP, group)

    # --- lifecycle -------------------------------------------------------------------------------------------------------------

    def _sendspin_info(self) -> dict:
        return {"device_id": self._load_settings()["device_id"], "client_id": self._sendspin_client_id, "name": self._sendspin_name()}

    def _make_sendspin_session(self, reader, writer, *, admit, on_status):
        settings = self._load_settings()
        holder = {}
        sink = PipeWireSink(volume=settings["sendspin_volume"], muted=settings["sendspin_muted"], log=decky.logger.info)  # one per session: a displaced
        session = SendspinSession(                                                                    # session must not close the new one's
            reader, writer, private_key=self._sendspin_private_key(), name=self._sendspin_name(), sink=sink,
            volume=settings["sendspin_volume"], muted=settings["sendspin_muted"], output_delay_ms=settings["sendspin_output_delay_ms"],
            on_status=on_status, on_settings=self._sendspin_settings_changed, admit=admit, log=decky.logger.info,
            available=codecs.available(), on_card=lambda snapshot: self._sendspin_card_changed(holder.get("session"), snapshot))
        holder["session"] = session
        return session

    # --- the media card (the Steam UI shows what is playing and sends the buttons back) -------------------------------------------

    def _sendspin_card_session(self):
        listener = self.sendspin_listener
        return listener.current if listener is not None else None

    def _sendspin_card_changed(self, session, snapshot: dict) -> None:
        """A session's card changed: emit it as the `sendspin_card` event. A session that was displaced by another server is
        ignored (its card must not replace the new one)."""
        if session is None or session is not self._sendspin_card_session():
            return
        task = asyncio.ensure_future(self._sendspin_emit_card(snapshot))
        self._sendspin_tasks.add(task)
        task.add_done_callback(self._sendspin_tasks.discard)

    async def _sendspin_emit_card(self, snapshot: dict) -> None:
        try:
            await decky.emit("sendspin_card", snapshot)
        except Exception as e:  # noqa: BLE001 - a missing frontend must not matter
            decky.logger.debug(f"Sendspin card event failed: {e!r}")

    async def get_sendspin_card(self) -> dict:
        """The card now (for a frontend that loads late); `{"active": False}` without a session."""
        session = self._sendspin_card_session()
        return session._card_snapshot() if session is not None else {"active": False}

    async def get_sendspin_card_config(self) -> dict:
        return {"port": self.http_port, "nonce": self.nonce}

    async def sendspin_card_command(self, name: str) -> dict:
        """A card button: previous, play, pause or next. `{"ok": True}` or `{"error": reason}`."""
        session = self._sendspin_card_session()
        if session is None:
            return {"error": "not connected"}
        if not isinstance(name, str) or name not in CARD_COMMANDS:
            return {"error": "unknown command"}
        if name not in session.card_commands:
            return {"error": "not offered"}
        now = time.monotonic()
        if now - self._card_command_at < CARD_COMMAND_MIN_INTERVAL_S:
            return {"error": "too fast"}  # a button held down or mashed must not become a storm of commands to Music Assistant
        if not await session.send_command(name):
            return {"error": "not offered"}
        self._card_command_at = now
        return {"ok": True}

    async def sendspin_card_volume(self, value) -> dict:
        """The card's slider: this device's player volume, 0-100."""
        session = self._sendspin_card_session()
        if session is None:
            return {"error": "not connected"}
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            return {"error": "volume must be 0-100"}
        await session.set_player_volume(round(value))
        return {"ok": True}

    def _sendspin_cover(self, cover_id: str):
        """The current cover when `cover_id` is its id (the loopback route), else None."""
        session = self._sendspin_card_session()
        return session.artwork.lookup(cover_id) if session is not None else None

    async def _sendspin_start(self) -> bool:
        """Start listening and advertising when enabled. False when disabled or when it could not start (status error)."""
        async with self._sendspin_lock:
            if not self._load_settings()["sendspin_enabled"]:
                self._push_sendspin()
                return False
            if self.sendspin_asleep:
                return False
            if self.sendspin_listener is not None:
                return True
            codecs.set_logger(decky.logger.warning)
            decky.logger.info(f"Sendspin codecs: {', '.join(sorted(codecs.available() | {'pcm'}, key=['flac', 'pcm', 'opus'].index))}")
            try:
                crypto.load()
                self._sendspin_client_id = client_id_for(crypto.x25519_public(self._sendspin_private_key()))
                self.sendspin_crypto_error = ""
            except crypto.CryptoUnavailable as e:
                self.sendspin_crypto_error = f"crypto unavailable: {e}"
                decky.logger.error(f"Sendspin cannot start: {self.sendspin_crypto_error}")
                self._push_sendspin()
                return False
            listener = SendspinListener(self._make_sendspin_session, on_change=lambda _l: self._push_sendspin(), start_port=LISTEN_PORT,
                                        log=decky.logger.info)
            self.sendspin_listener = listener
            if not await listener.start():
                decky.logger.warning(f"Sendspin cannot listen: {listener.error}")
                self._push_sendspin()
                return False
            self.sendspin_advertiser = sendspin_advertiser(self._sendspin_info, listener.port)
            await self.sendspin_advertiser.start()
            decky.logger.info(f"Sendspin player '{self._sendspin_name()}' listening on port {listener.port}")
            self._push_sendspin()
            return True

    async def _sendspin_stop(self, reason: str = "shutdown", push: bool = True) -> None:
        """Withdraw the mDNS record first (so no server connects any more), then say goodbye to the connected server."""
        async with self._sendspin_lock:
            advertiser, listener = self.sendspin_advertiser, self.sendspin_listener
            self.sendspin_advertiser = self.sendspin_listener = None
            self._sendspin_stopping = (advertiser, listener)  # what an abort has to kill if this does not finish
            try:
                if advertiser is not None:
                    await advertiser.stop()
                if listener is not None:
                    await listener.stop(reason)
            finally:
                self._sendspin_stopping = (None, None)
            self._sendspin_flush_settings()
        if push:
            self._push_sendspin()

    def _sendspin_abort(self) -> None:
        """Synchronous stop for an unload (see Plugin._unload_now): kill the audio output, close the connections and withdraw the
        mDNS record without waiting for anything; also what an interrupted `_sendspin_stop` still had open."""
        stopping_advertiser, stopping_listener = getattr(self, "_sendspin_stopping", (None, None))
        for holder in (self.sendspin_listener, stopping_listener, self.sendspin_advertiser, stopping_advertiser):
            if holder is not None:
                try:
                    holder.abort()
                except Exception as e:  # noqa: BLE001
                    decky.logger.warning(f"Sendspin abort failed: {e!r}")
        self.sendspin_listener = self.sendspin_advertiser = None

    # --- settings written by the server ------------------------------------------------------------------------------------------

    def _sendspin_settings_changed(self, **changes) -> None:
        self._sendspin_pending.update({SETTING_KEYS[k]: v for k, v in changes.items() if k in SETTING_KEYS})
        if self._sendspin_save_handle is not None:
            self._sendspin_save_handle.cancel()
        self._sendspin_save_handle = asyncio.get_event_loop().call_later(SETTINGS_DEBOUNCE_S, self._sendspin_flush_settings)

    def _sendspin_flush_settings(self) -> None:
        if self._sendspin_save_handle is not None:
            self._sendspin_save_handle.cancel()
            self._sendspin_save_handle = None
        pending, self._sendspin_pending = self._sendspin_pending, {}
        if pending:
            try:
                self._save_sendspin(**pending)
            except (ValueError, OSError) as e:
                decky.logger.warning(f"Could not save the Sendspin level: {e}")

    # --- command ---------------------------------------------------------------------------------------------------------------

    async def _cmd_sendspin_enable(self, data: dict) -> dict:
        enabled = m.sendspin_enable_request(data)
        try:
            self._save_sendspin(sendspin_enabled=enabled)
        except OSError as e:
            raise m.ProtocolError(m.INTERNAL, f"could not save the setting: {e}") from None
        if enabled:
            await self._sendspin_start()
        else:
            await self._sendspin_stop("user_request")
        self._push_sendspin()
        return {}

    async def _sendspin_boot(self) -> None:
        """At plugin start: publish the group and start when enabled; never raises."""
        self._push_sendspin()
        try:
            await self._sendspin_start()
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"Sendspin failed to start: {e!r}")

    # --- sleep, shutdown and wake ----------------------------------------------------------------------------------------------

    async def _sendspin_sleep(self, reason: str) -> None:
        """Called with the sleep/shutdown announcement: stop the stream, say goodbye, withdraw the record, within a short
        deadline. Nothing is pushed (the device is going down); the resume refresh pushes the real status."""
        self.sendspin_asleep = True
        if self.sendspin_listener is None and self.sendspin_advertiser is None:
            return
        try:
            await asyncio.wait_for(self._sendspin_stop(reason, push=False), SLEEP_STOP_TIMEOUT_S)
        except asyncio.TimeoutError:
            decky.logger.warning("Sendspin did not stop in time for the sleep")
        except Exception as e:  # noqa: BLE001
            decky.logger.warning(f"Sendspin stop for the sleep failed: {e!r}")

    async def _sendspin_wake(self) -> None:
        """After the resume refresh (the network wait is done): start again from scratch. A connection that survived a
        suspend nobody announced is closed first, and the mDNS record is rebuilt (the multicast membership is gone)."""
        self.sendspin_asleep = False
        try:
            if self.sendspin_listener is not None:
                await self._sendspin_stop("restart", push=False)
            await self._sendspin_start()
        except Exception as e:  # noqa: BLE001
            decky.logger.error(f"Sendspin failed to start after the resume: {e!r}")
        self._push_sendspin()
