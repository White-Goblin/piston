"""Loopback HTTP server for the PiP player page (static files only; signalling goes through the plugin)."""
import asyncio
import json
import os
import secrets
from urllib.parse import parse_qs, urlsplit

import decky



# Local server for the PiP player page (loopback only)
LOCAL_PORT = 47820
STATIC_FILES = {
    "/player.html": "text/html; charset=utf-8",
    "/player.js": "application/javascript; charset=utf-8",
}

MAX_BODY = 64_000


class HttpServerMixin:
    def _http_response(self, status: str, content_type: str, body: bytes) -> bytes:
        head = (
            f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        )
        return head.encode() + body

    def _json_response(self, status: str, data: dict) -> bytes:
        return self._http_response(status, "application/json", json.dumps(data).encode())

    async def _handle_request(self, method: str, target: str, body: bytes = b"") -> bytes:
        url = urlsplit(target)
        text = "text/plain; charset=utf-8"
        if method == "GET" and url.path in STATIC_FILES:
            path = os.path.join(decky.DECKY_PLUGIN_DIR, "dist", url.path.lstrip("/"))
            try:
                with open(path, "rb") as f:
                    return self._http_response("200 OK", STATIC_FILES[url.path], f.read())
            except FileNotFoundError:
                return self._http_response("404 Not Found", text, b"not found")
        if url.path in ("/pip/events", "/pip/send"):
            return await self._handle_pip_request(method, url, body)
        return self._http_response("404 Not Found", text, b"not found")

    async def _handle_pip_request(self, method: str, url, body: bytes) -> bytes:
        """Signalling endpoints for the player page; protected by the per-run nonce."""
        query = parse_qs(url.query)
        if not secrets.compare_digest(query.get("nonce", [""])[0], self.nonce):
            return self._http_response("403 Forbidden", "text/plain", b"bad nonce")
        session = query.get("session", [""])[0]
        if url.path == "/pip/events" and method == "GET":
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                return self._http_response("400 Bad Request", "text/plain", b"bad after")
            return self._json_response("200 OK", await self.pip_poll(session, after))
        if url.path == "/pip/send" and method == "POST":
            try:
                message = json.loads(body or b"{}")
                if not isinstance(message, dict):
                    raise ValueError("object expected")
                known = self.pip_send(session, message)
            except ValueError as e:
                return self._http_response("400 Bad Request", "text/plain", str(e).encode())
            return self._json_response("200 OK" if known else "404 Not Found", {"ok": known})
        return self._http_response("405 Method Not Allowed", "text/plain", b"")

    async def _serve_client(self, reader, writer):
        try:
            request_line = await asyncio.wait_for(reader.readline(), 5)
            parts = request_line.decode(errors="replace").split()
            headers = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), 5)
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode(errors="replace").partition(":")
                headers[key.strip().lower()] = value.strip()
            if len(parts) < 2 or parts[0] not in ("GET", "POST"):
                response = self._http_response("405 Method Not Allowed", "text/plain", b"")
            else:
                length = int(headers.get("content-length", "0") or 0)
                if not 0 <= length <= MAX_BODY:
                    response = self._http_response("413 Payload Too Large", "text/plain", b"")
                else:
                    body = await asyncio.wait_for(reader.readexactly(length), 5) if length else b""
                    response = await self._handle_request(parts[0], parts[1], body)
            writer.write(response)
            await writer.drain()
        except Exception as e:
            decky.logger.warning(f"Local server request failed: {e}")
        finally:
            writer.close()

    async def _start_http_server(self):
        # Fall back to a free port if the default is taken
        for port in (LOCAL_PORT, 0):
            try:
                self.http_server = await asyncio.start_server(self._serve_client, "127.0.0.1", port)
                self.http_port = self.http_server.sockets[0].getsockname()[1]
                decky.logger.info(f"Local server listening on 127.0.0.1:{self.http_port}")
                return
            except OSError as e:
                decky.logger.warning(f"Local server could not bind port {port}: {e}")
