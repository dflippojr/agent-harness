"""A local upstream that fails on purpose, for tests/test_outbound_failures.py (#733).

`Stub` listens on 127.0.0.1 and answers every connection the way its `mode` says. `redirect` points every httpx
request in the process at it, whatever host the code under test names, so nothing leaves the machine.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx

MODES = ("refuse", "timeout", "4xx", "5xx", "malformed", "truncated")

READ_CAP = 0.3   # seconds: every httpx read, write and pool timeout is cut to this in the timeout mode
HOLD = 1.5       # seconds the timeout mode keeps a connection open without answering, then hangs up
CONNECT_CAP = 2.0


def closed_port() -> int:
    """A loopback port nothing listens on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _response(status: str, body: bytes, ctype: str, length: int | None = None) -> bytes:
    head = (f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body) if length is None else length}"
            "\r\nConnection: close\r\n\r\n")
    return head.encode() + body


ANSWERS = {
    "4xx": _response("403 Forbidden", b'{"error": {"message": "forbidden by the stub"}, "message": "forbidden"}',
                     "application/json"),
    "5xx": _response("503 Service Unavailable", b"upstream unavailable", "text/plain"),
    "malformed": _response("200 OK", b"<html>this is not the JSON you asked for", "application/json"),
    # The headers promise 4096 bytes; the connection closes after 11.
    "truncated": _response("200 OK", b'{"ok": true,', "application/json", length=4096),
}


class Stub:
    """One listening socket; `mode` decides every answer. `hits` counts connections, `heads` keeps request heads."""

    def __init__(self):
        self.mode = "5xx"
        self.hits = 0
        self.heads: list[bytes] = []
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._sock = socket.create_server(("127.0.0.1", 0))
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="outbound-stub", daemon=True)
        self._thread.start()

    def reset(self, mode: str) -> None:
        with self._lock:
            self.mode, self.hits, self.heads = mode, 0, []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def received(self, text: str) -> bool:
        with self._lock:
            return any(text.encode() in head for head in self.heads)

    def close(self) -> None:
        self._closing.set()
        self._sock.close()
        self._thread.join(5)

    def _serve(self) -> None:
        while not self._closing.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _read_head(self, conn: socket.socket) -> bytes:
        """The request head and body; a client that sends no HTTP gets 0.3 s to say what it has."""
        conn.settimeout(0.3)
        data = b""
        try:
            while len(data) < 65536 and not self._complete(data):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        return data

    @staticmethod
    def _complete(data: bytes) -> bool:
        head, sep, body = data.partition(b"\r\n\r\n")
        if not sep:
            return False
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                return len(body) >= int(value.strip() or 0)
        return True  # no body, or a chunked one whose first chunk is enough

    def _answer(self, conn: socket.socket) -> None:
        with conn:
            head = self._read_head(conn)
            with self._lock:
                self.hits += 1
                self.heads.append(head)
                mode = self.mode
            try:
                if mode == "timeout":
                    conn.settimeout(0.05)
                    end = time.monotonic() + HOLD
                    while time.monotonic() < end and not self._closing.is_set():
                        try:
                            if not conn.recv(4096):
                                return  # the client gave up
                        except TimeoutError:
                            continue
                    return
                conn.sendall(ANSWERS.get(mode, ANSWERS["5xx"]))
                conn.shutdown(socket.SHUT_WR)
                time.sleep(0.05)  # let the client read before the close
            except OSError:
                pass


class Redirect:
    """Counts and reroutes every request httpx's real transports send. `refuse` fails the connection the way a
    closed port does, without the two seconds Windows spends retrying one."""

    def __init__(self, stub: Stub):
        self.stub = stub
        self.count = 0
        self.unbounded: list[str] = []   # requests sent with no read timeout at all
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.count, self.unbounded = 0, []

    def rewrite(self, request: httpx.Request) -> None:
        with self._lock:
            self.count += 1
        timeouts = dict(request.extensions.get("timeout") or {})
        if timeouts.get("read", 0) is None:
            with self._lock:
                self.unbounded.append(str(request.url.copy_with(query=None)))
        if self.stub.mode == "refuse":
            raise httpx.ConnectError("[outbound stub] connection refused", request=request)
        cap = READ_CAP if self.stub.mode == "timeout" else 10.0
        request.extensions["timeout"] = {
            "connect": min(timeouts.get("connect") or CONNECT_CAP, CONNECT_CAP),
            **{key: min(timeouts.get(key) or cap, cap) for key in ("read", "write", "pool")}}
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.stub.port)


def redirect(monkeypatch, stub: Stub) -> Redirect:
    """Send every httpx request in the process to `stub` (MockTransport and TestClient stay in-process)."""
    rerouted = Redirect(stub)
    sync_send, async_send = httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request

    def handle_request(self, request):
        rerouted.rewrite(request)
        return sync_send(self, request)

    async def handle_async_request(self, request):
        rerouted.rewrite(request)
        return await async_send(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request)
    return rerouted
