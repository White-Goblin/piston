"""Accepts the Sendspin server's connections (server-initiated mode): binds 8928 or the next free port, upgrades
`/sendspin` websockets (refusing an `Origin` header), and lets one connection at a time be admitted, ranked by the spec's
activity rule: playback outranks pairing, which outranks no activity; a higher or equal newcomer displaces the current
connection (`client/goodbye another_server`), a lower one is rejected (`concurrent_attempt`).

Status for the Piston `sendspin` group: waiting (listening, nobody connected), connected, streaming, error."""
import asyncio
import errno
from typing import Callable

from .. import wsproto

DEFAULT_PORT = 8928
PORT_TRIES = 10
PATH = "/sendspin"
MAX_CONNECTIONS = 5  # the admitted one plus a few not admitted yet (unauthenticated, so bounded)
STOP_GRACE_S = 3
ACTIVITY_RANK = {"playback": 2, "pairing": 1}


def rank(activities) -> int:
    return max((ACTIVITY_RANK.get(a, 0) for a in activities), default=0)


class SendspinListener:
    """`make_session(reader, writer, *, admit, on_status)` builds a SendspinSession (the mixin binds identity, sink, settings).
    `on_change(listener)` is called whenever status, server name, port or error changes."""

    def __init__(self, make_session: Callable, on_change: Callable | None = None, start_port: int = DEFAULT_PORT, tries: int = PORT_TRIES,
                 host: str = "0.0.0.0", max_connections: int = MAX_CONNECTIONS, log: Callable[[str], None] = lambda m: None):
        self._make_session, self._on_change = make_session, on_change or (lambda listener: None)
        self._start_port, self._tries, self._host = start_port, tries, host
        self._max_connections = max_connections
        self._log = log
        self._server = None
        self._tasks = set()
        self.current = None
        self.port = 0
        self.status = "stopped"
        self.server_name = None
        self.error = None

    # --- lifecycle -------------------------------------------------------------------------------------------------------------

    async def start(self) -> bool:
        """Bind the first free port. False (status `error`) when none is free."""
        if self._server is not None:
            return True
        ports = [0] if self._start_port == 0 else range(self._start_port, self._start_port + self._tries)
        last = None
        for port in ports:
            try:
                self._server = await asyncio.start_server(self._accept, self._host, port)
            except OSError as e:
                last = e
                if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                    break
                continue
            self.port = self._server.sockets[0].getsockname()[1]
            if port != self._start_port and self._start_port:
                self._log(f"sendspin: port {self._start_port} is busy, listening on {self.port}")
            self._set(status="waiting", server_name=None, error=None)
            return True
        self.port = 0
        self._set(status="error", server_name=None,
                  error=f"no free port from {self._start_port} to {self._start_port + self._tries - 1}" if self._start_port else f"cannot listen: {last}")
        return False

    async def stop(self, reason: str = "shutdown") -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        sessions = [t.session for t in self._tasks if getattr(t, "session", None) is not None]
        for session in sessions:
            session.request_stop(reason)
        if self._tasks:
            _, pending = await asyncio.wait(list(self._tasks), timeout=STOP_GRACE_S)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
        self.current = None
        self.port = 0
        self._set(status="stopped", server_name=None, error=None)

    # --- state -----------------------------------------------------------------------------------------------------------------

    def _set(self, **changes) -> None:
        before = (self.status, self.server_name, self.port, self.error)
        for key, value in changes.items():
            setattr(self, key, value)
        if (self.status, self.server_name, self.port, self.error) != before:
            try:
                self._on_change(self)
            except Exception as e:  # noqa: BLE001 - a broken observer must not break connections
                self._log(f"sendspin: status callback failed: {e!r}")

    def _session_status(self, session, status: str, server_name=None, error=None, **_) -> None:
        if session is not self.current:
            return  # a displaced or never-admitted connection
        if status == "waiting":
            self.current = None
            self._set(status="waiting" if self._server is not None else "stopped", server_name=None, error=None)
        elif status == "error":
            self._set(status="error", server_name=server_name, error=error)
        else:
            self._set(status=status, server_name=server_name, error=None)

    async def _admit(self, session):
        """The arbitration rule; returns None to accept or the goodbye reason to reject."""
        current = self.current
        if current is not None and current is not session:
            if rank(session.activities) < rank(current.activities):
                return "concurrent_attempt"
            current.request_stop("another_server")
        self.current = session
        return None

    # --- connections -----------------------------------------------------------------------------------------------------------

    async def _accept(self, reader, writer) -> None:
        if len(self._tasks) >= self._max_connections:
            writer.close()
            return
        task = asyncio.current_task()
        task.session = None
        self._tasks.add(task)
        try:
            if not await wsproto.handshake(reader, writer, PATH, forbid=("origin",)):
                writer.close()
                return
            holder = {}
            session = self._make_session(reader, writer, admit=self._admit,
                                         on_status=lambda status, **info: self._session_status(holder.get("session"), status, **info))
            holder["session"] = task.session = session
            self._log(f"sendspin: connection ended: {await session.run()}")
        except Exception as e:  # noqa: BLE001
            self._log(f"sendspin: connection failed: {e!r}")
        finally:
            if task.session is not None and task.session is self.current:
                self._session_status(task.session, "waiting")
            self._tasks.discard(task)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
