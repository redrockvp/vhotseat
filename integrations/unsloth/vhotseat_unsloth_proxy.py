"""A vhotseat gate in front of Unsloth Studio, for clients that will point at it.

Studio has no pre-load hook, no plugin and no webhook: nothing inside it asks before it
allocates 20 GB. So this takes the place of that asking. Clients that talk to the proxy
(vhotseat's port) get the card before their model loads; a chat request that would load a
model is a load request.

    you, pi, scripts  ──►  this proxy :8890  ──►  Studio :8888
                              │
                              └─ seat.acquire("unsloth") before any request that loads

The Studio desktop app still talks to :8888 directly and cannot be moved behind this: it
starts its own backend hard-bound to that port. Those loads are covered by vhotseat's
reactive arm instead (it watches where the card's memory comes from), not by this file.

Run:

    python vhotseat_unsloth_proxy.py [--port 8890] [--upstream http://127.0.0.1:8888]
                                     [--workload unsloth] [--hold 120]

Auth is passed through, not injected: a client that can reach :8890 could load any model
and fill the RAM, so it has to already have Studio's key. `estimate` and `unload` are not
gated: they load nothing, and refusing them would stop a client from cleaning up.
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

NAME = "unsloth"
HOLD_S = 120.0

# Requests that put weights on the card. A chat request naming a model Studio hasn't loaded
# loads it (multi-model off: it swaps whatever is there for the one asked for), so the whole
# completion family is a load request, exactly as it is for Ollama.
LOAD_PREFIXES = ("/api/inference/load", "/v1/load", "/v1/chat/completions", "/v1/completions",
                 "/v1/responses", "/v1/messages", "/v1/embeddings", "/api/inference/chat",
                 "/api/inference/images/load", "/api/inference/video/load",
                 "/api/inference/audio/stt/load", "/api/settings/embedding-model")
# …except these: they answer about loads without performing one.
NOT_LOAD = ("/api/inference/load-progress", "/api/inference/loaded-models",
            "/api/inference/estimate-memory", "/api/models/cached-gguf")


def loads_a_model(method: str, path: str) -> bool:
    """Whether this request can put weights on the card, and so must hold the card first.
    Anything that only reports on a load, or ends one, is not one."""
    if method != "POST" or path.endswith(("/load-progress", "/unload")):
        return False
    if any(path.startswith(p) for p in NOT_LOAD):
        return False
    return any(path.startswith(p) for p in LOAD_PREFIXES) or path.endswith("/load")


class State:
    """What's going through the proxy right now. Per request, not per connection: a
    keep-alive connection opened before a drain must not carry a request after it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.in_flight = 0
        self.draining = False

    def enter(self):
        with self.lock:
            self.in_flight += 1

    def leave(self):
        with self.lock:
            self.in_flight -= 1

    def snapshot(self) -> tuple[int, bool]:
        with self.lock:
            return self.in_flight, self.draining

    def set_draining(self, on: bool):
        with self.lock:
            self.draining = on


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True      # else keep-alive clients wait ~40 ms per request
    seat = None                         # set by the server (a vhotseat api.Seat)
    state: State
    upstream = ""
    workload = NAME
    hold = HOLD_S
    unload = None                       # callable() -> unload Studio's models; see drain()

    def log_message(self, *a):
        pass

    # ---- responses -------------------------------------------------------------

    def _reply(self, code: int, obj: dict):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _busy(self, why: str):
        """In the server's own shape, with a Retry-After: agent harnesses back off on 503,
        and a render can outlast a hold, so tell them when to come back."""
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", "30")
        raw = json.dumps({"error": f"GPU busy: {why}", "detail": why}).encode()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # ---- the two endpoints vhotseat calls --------------------------------------

    def _vhotseat(self, path: str, method: str) -> bool:
        if path == "/_vhotseat/status":
            n, draining = self.state.snapshot()
            self._reply(200, {"in_flight": n, "draining": draining})
            return True
        if path == "/_vhotseat/drain" and method == "POST":
            wait = float((parse_qs(urlsplit(self.path).query).get("wait") or ["30"])[0])
            self.state.set_draining(True)
            deadline = time.monotonic() + wait
            while self.state.snapshot()[0] and time.monotonic() < deadline:
                time.sleep(0.05)
            still, _ = self.state.snapshot()
            if still:
                self.state.set_draining(False)      # a drain that never clears is a stuck proxy
                self._reply(409, {"status": "busy", "in_flight": still})
                return True
            if self.unload:
                self.unload()
            self.state.set_draining(False)          # what waited now goes through acquire()
            self._reply(200, {"in_flight": 0, "drained": True})
            return True
        return False

    # ---- request handling ------------------------------------------------------

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PUT(self):
        self._handle("PUT")

    def _handle(self, method: str):
        url = urlsplit(self.path)
        if self._vhotseat(url.path, method):
            return
        if self._is_upgrade():
            # A websocket: splice raw bytes. It loads nothing itself; the request that opens
            # it is the one that was gated.
            return self._tunnel()
        body = self._read_body()
        path = url.path
        self.sent = False

        # Counted from before anything is decided. If the count only started once the request
        # was already forwarded, a drain that ran while this one was still waiting on the card
        # saw nobody in flight, unloaded the server and handed the card on - and then this
        # request loaded its model on top of whoever had just been given the GPU.
        self.state.enter()
        try:
            if self.state.snapshot()[1]:
                # Draining: vhotseat is taking the card from this workload. Wait, then 503.
                deadline = time.monotonic() + self.hold
                while self.state.snapshot()[1] and time.monotonic() < deadline:
                    time.sleep(0.25)
                if self.state.snapshot()[1]:
                    return self._busy(f"{self.workload} is giving up the GPU (waited {int(self.hold)} s)")

            if loads_a_model(method, path):
                # The fast path first: while nothing has changed hands since the last check this
                # is a file read, no lock and no probe - and a cold check probes this workload
                # too, which asks this same server its /_vhotseat/status question, so never do
                # either while holding something this handler needs.
                if not self.seat.owns(self.workload):
                    try:
                        self.seat.acquire(self.workload, wait=self.hold)
                    except Exception as exc:          # api.Busy, api.SwitchError, …
                        return self._busy(str(exc))
            try:
                self._forward(method, self.path, body)   # self.path, query string and all
            except (OSError, http.client.HTTPException) as exc:
                # Upstream gone before anything reached the client: a 502 the client can act on,
                # not a dropped connection. Once bytes are out there is nothing left to say.
                if not self.sent:
                    self._reply(502, {"error": f"{self.upstream} unreachable: {exc}"})
        finally:
            self.state.leave()                          # to the last byte, whatever path it took

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _is_upgrade(self) -> bool:
        return (self.headers.get("upgrade") or "").lower() in ("websocket", "websocket;")

    def _forward(self, method: str, path: str, body: bytes):
        up = urlsplit(self.upstream)
        conn = http.client.HTTPConnection(up.hostname, up.port, timeout=None)
        conn.request(method, path, body=body or None,
                     headers={k: v for k, v in self.headers.items()
                              if k.lower() not in ("host", "content-length",
                                                   "connection", "transfer-encoding")})
        resp = conn.getresponse()
        try:
            self.send_response(resp.status, resp.reason or "")
            length = resp.getheader("Content-Length")
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "content-length", "connection"):
                    continue
                self.send_header(k, v)
            if length is not None:
                self.send_header("Content-Length", length)
            else:
                # Re-framed: without a length the client can't tell where the body ends, so
                # a stream gets chunked and the close of the upstream stays our business.
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.sent = True
            if length is None:
                while True:                   # stream it: a buffered proxy shows a chat
                    part = resp.read(8192)    # client nothing until the answer is over
                    if not part:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            else:
                left = int(length)
                while left > 0:
                    part = resp.read(min(8192, left))
                    if not part:
                        break
                    left -= len(part)
                    self.wfile.write(part)
                    self.wfile.flush()
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass                                  # the client hung up mid-answer
        finally:
            conn.close()                            # the caller counts the request finished

    def _tunnel(self):
        """Raw byte splice for an upgraded connection (a websocket from a UI on this port).
        Only the handshake headers are re-sent; anything the client buffered behind them is
        not visible here, which is fine for a handshake and not for much else.

        Deliberately not counted in in_flight: a UI tab holds its websocket open for as long
        as it is open, and counting it would make every drain answer 409 for as long as
        somebody had a window on the screen - the card could never be taken back."""
        up = urlsplit(self.upstream)
        try:
            remote = socket.create_connection((up.hostname, up.port), timeout=10)
        except OSError:
            return self._reply(502, {"error": "upstream unavailable"})
        head = f"{self.command} {self.path} HTTP/1.1\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in self.headers.items())
        remote.sendall(head.encode() + b"\r\n")

        def pipe(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        try:
            client = self.connection
            up_thread = threading.Thread(target=pipe, args=(client, remote), daemon=True)
            up_thread.start()
            pipe(remote, client)
            up_thread.join(timeout=2)
        finally:
            try:
                remote.close()
            except OSError:
                pass


class Proxy:
    """The server, wrapped so tests can start it on a free port with a test seat."""

    def __init__(self, seat, upstream: str, port: int = 0, workload: str = NAME,
                 hold: float = HOLD_S, unload=None):
        self.state = State()
        handler = type("Bound", (Handler,), {"seat": seat, "state": self.state, "upstream": upstream,
                                            "workload": workload, "hold": hold, "unload": staticmethod(unload)})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def status(self) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        conn.request("GET", "/_vhotseat/status")
        body = conn.getresponse().read()
        conn.close()
        return json.loads(body)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8890)
    ap.add_argument("--upstream", default="http://127.0.0.1:8888")
    ap.add_argument("--workload", default=NAME, help="the name in vhotseat's config")
    ap.add_argument("--hold", type=float, default=HOLD_S,
                    help="seconds a gated request waits for the card before it gets 503")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    from vhotseat import api
    seat = api.open(args.config)
    proxy = Proxy(seat, args.upstream, args.port, args.workload, args.hold,
                  unload=lambda: _unload_models(args.upstream)).start()
    print(f"vhotseat gate for {args.workload}: {proxy.url} -> {args.upstream}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        proxy.stop()
    return 0


def _unload_models(upstream: str):
    """Ask Studio to let go of what it has loaded, by model_path (what loaded-models reports)."""
    up = urlsplit(upstream)
    try:
        conn = http.client.HTTPConnection(up.hostname, up.port, timeout=10)
        conn.request("GET", "/api/inference/loaded-models")
        rows = json.loads(conn.getresponse().read()).get("data", [])
        for row in rows:
            ident = row.get("id") or row.get("model_path")
            if ident:
                conn.request("POST", "/api/inference/unload",
                             body=json.dumps({"model_path": ident}),
                             headers={"Content-Type": "application/json"})
                conn.getresponse().read()
        conn.close()
    except (OSError, ValueError):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
