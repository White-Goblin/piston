"""Signalling between the PiP player page and the integration (no Home Assistant credential on the device).

Player page <-> (loopback HTTP long-poll) <-> plugin backend <-> (protocol) <-> integration <-> camera.
Player to integration: protocol event `pip_signal` {session, kind, ...}.
Integration to player: protocol command `pip_signal` {session, kind, ...}, queued here for the player to poll.
"""
import asyncio
import re

from .proto import messages as m

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
FROM_PLAYER_KINDS = ("start", "offer", "candidate", "hls", "close")
MAX_QUEUE = 200
MAX_MESSAGE_BYTES = 64_000
POLL_SECONDS = 25


class _Session:
    def __init__(self):
        self.queue: list = []  # [(seq, message)]
        self.seq = 0
        self.closed = False
        self.changed = asyncio.Event()


class PipSignalMixin:
    def _init_pip_signal(self) -> None:
        self.pip_sessions: dict[str, _Session] = {}

    # --- session lifecycle ---------------------------------------------------

    def _pip_open(self, session: str) -> None:
        if not SESSION_RE.match(session):
            raise m.ProtocolError(m.BAD_REQUEST, "session must be 8-64 letters, digits, - or _")
        self._pip_close_all(notify=True)  # only one PiP window exists
        self.pip_sessions[session] = _Session()

    def _pip_close(self, session: str, notify: bool) -> None:
        s = self.pip_sessions.pop(session, None)
        if s is None:
            return
        s.closed = True
        s.changed.set()
        if notify:
            self._push_event("pip_signal", {"session": session, "kind": "close"})

    def _pip_close_all(self, notify: bool) -> None:
        for session in list(self.pip_sessions):
            self._pip_close(session, notify)

    # --- integration -> player ----------------------------------------------

    async def _cmd_pip_signal(self, data: dict) -> dict:
        session = self.pip_sessions.get(str(data.get("session", "")))
        if session is None:
            raise m.ProtocolError(m.BAD_REQUEST, "unknown or closed PiP session")
        message = {k: v for k, v in data.items() if k != "session"}
        if not isinstance(message.get("kind"), str):
            raise m.ProtocolError(m.BAD_REQUEST, "kind is required")
        if len(session.queue) >= MAX_QUEUE:
            raise m.ProtocolError(m.BUSY, "the player is not keeping up")
        session.seq += 1
        session.queue.append((session.seq, message))
        session.changed.set()
        return {}

    async def pip_poll(self, session_id: str, after: int, timeout: float = POLL_SECONDS) -> dict:
        """Messages newer than `after`, waiting up to `timeout` seconds for the first one."""
        session = self.pip_sessions.get(session_id)
        if session is None:
            return {"messages": [], "next": after, "closed": True}
        deadline = asyncio.get_running_loop().time() + timeout
        while not session.closed:
            fresh = [(seq, msg) for seq, msg in session.queue if seq > after]
            if fresh:
                session.queue = [(seq, msg) for seq, msg in session.queue if seq > after]
                return {"messages": [{"seq": seq, **msg} for seq, msg in fresh], "next": fresh[-1][0], "closed": False}
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            session.changed.clear()
            try:
                await asyncio.wait_for(session.changed.wait(), remaining)
            except asyncio.TimeoutError:
                break
        return {"messages": [], "next": after, "closed": session.closed}

    # --- player -> integration ----------------------------------------------

    def pip_send(self, session_id: str, message: dict) -> bool:
        """Forward a player message to the integration. False when the session is unknown."""
        if session_id not in self.pip_sessions:
            return False
        kind = message.get("kind")
        if kind not in FROM_PLAYER_KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        payload = {"session": session_id, **{k: v for k, v in message.items() if k != "session"}}
        if kind == "close":
            self._pip_close(session_id, notify=False)
        self._push_event("pip_signal", payload)
        return True
