"""A scripted Sendspin server for tests: connects to the player over a real websocket, runs the Noise initiator with the
same code the player's tests use, answers time requests and lets the test send messages and audio."""
import asyncio
import json

from piston_device.sendspin import crypto, messages as sm, noise
from piston_device.sendspin.session import monotonic_us
from tests.wsclient import WsTestClient

SERVER_OFFSET_US = 7_000_000  # server clock = local monotonic clock + this


class FakeServer:
    def __init__(self, private_key: bytes | None = None):
        self.private = private_key or crypto.generate_private_key()
        self.id = sm.client_id_for(crypto.x25519_public(self.private))
        self.inbox = asyncio.Queue()
        self.client_init = None
        self.client_id = None
        self.answer_time = True
        self.time_requests = 0
        self.server_init_text = None

    def server_now(self) -> int:
        return monotonic_us() + SERVER_OFFSET_US

    async def connect(self, port: int, path: str = "/sendspin", headers: dict | None = None) -> None:
        self.ws = await WsTestClient.connect(port, path, headers)

    async def _text(self, timeout: float = 5) -> str:
        op, data = await self.ws.recv_raw(timeout)
        assert op == 1, f"expected text, got opcode {op}"
        return data.decode()

    async def handshake(self, *, wrong_client_key: bool = False, psk: bytes = noise.SENTINEL_PSK, category: str = "sn") -> None:
        self.client_init = await self._text()
        self.client_id = json.loads(self.client_init)["payload"]["client_id"]
        self.server_init_text = json.dumps({"type": "server/init", "payload": {"server_id": self.id, "version": 1}}, separators=(",", ":"))
        await self.ws.send(self.server_init_text)
        client_pub = sm.public_key_from_id(self.client_id)
        if wrong_client_key:
            client_pub = crypto.x25519_public(crypto.generate_private_key())
        initiator = noise.NoiseInitiator(self.private, client_pub, sm.prologue(self.client_init, self.server_init_text))
        payload = json.dumps({"psk_id": noise.psk_id_for(psk), "psk_category": category}).encode()
        await self.ws.send(sm.build_noise_handshake(initiator.write_message_1(payload)))
        reply = await self._text()
        _, self.transport = initiator.read_message_2(sm.parse_noise_handshake(reply), psk)

    # --- encrypted traffic --------------------------------------------------------------------------------------------------

    async def send(self, message_type: str, payload: dict) -> None:
        await self.ws.send_binary(self.transport.send(sm.json_frame(message_type, payload)))

    async def send_audio(self, timestamp_us: int, pcm: bytes) -> None:
        await self.ws.send_binary(self.transport.send(sm.build_audio_chunk(timestamp_us, pcm)))

    async def read(self, timeout: float = 5) -> tuple:
        op, data = await self.ws.recv_raw(timeout)
        if op == 8:
            return "close", None
        assert op == 2, f"expected binary, got opcode {op}"
        plain = self.transport.receive(data)
        obj = json.loads(plain[1:])
        return obj["type"], obj["payload"]

    async def _pump(self) -> None:
        """Answers client/time and queues everything else."""
        try:
            while True:
                message_type, payload = await self.read(timeout=60)
                if message_type == "client/time":
                    self.time_requests += 1
                    if self.answer_time:
                        now = self.server_now()
                        await self.send("server/time", {"client_transmitted": payload["client_transmitted"], "server_received": now,
                                                        "server_transmitted": now + 10})
                else:
                    await self.inbox.put((message_type, payload))
                    if message_type == "close":
                        return
        except (asyncio.CancelledError, asyncio.TimeoutError, OSError, AssertionError, noise.NoiseError):
            return

    def start_pump(self) -> None:
        self.pump = asyncio.create_task(self._pump())

    async def next(self, wanted: str | None = None, timeout: float = 5) -> tuple:
        """The next queued client message (optionally skipping others until `wanted`)."""
        while True:
            message_type, payload = await asyncio.wait_for(self.inbox.get(), timeout)
            if wanted is None or message_type == wanted:
                return message_type, payload

    async def hello_and_activate(self, activities=("playback",), roles=("player@v1",)) -> dict:
        await self.send("server/hello", {"name": "Test Server"})
        hello = await self.read()
        assert hello[0] == "client/hello", hello
        await self.send("server/activate", {"activities": list(activities), "active_roles": list(roles)})
        return hello[1]

    def close(self) -> None:
        if getattr(self, "pump", None):
            self.pump.cancel()
        self.ws.close()
