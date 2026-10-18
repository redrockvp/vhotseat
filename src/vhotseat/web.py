"""`vhotseat web`: a dashboard of RAM and VRAM over time, who owned the GPU,
and the event and log history, served from telemetry.db.

The page (web.html) polls a small JSON API:

  GET /api/status                    the live status (`status --json`), plus the workloads' order and
                                     the card's model, load, temperature and power
  GET /api/history?since=&until=     samples between two Unix times, averaged into at most `points` buckets
  GET /api/events?since=&until=      events, newest first
  GET /api/logs?since=&until=        vhotseat's log lines from every process, newest first

It only reads: nothing here switches or releases anything. While it runs it
also records samples, like `vhotseat record`.
"""

import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import util
from .telemetry import Sampler

# Probing calls every workload's API; several open tabs share one probe a second.
STATUS_TTL_S = 1.0


def bucket(samples: list[dict], points: int) -> list[dict]:
    """Average samples into at most `points` time buckets. A bucket's mode is its
    last sample's, and the time is its last sample's too."""
    if len(samples) <= points:
        return samples
    t0, t1 = samples[0]["t"], samples[-1]["t"]
    width = (t1 - t0) / points or 1
    groups: dict[int, list[dict]] = {}
    for s in samples:
        groups.setdefault(min(int((s["t"] - t0) / width), points - 1), []).append(s)
    return [_mean(g) for _, g in sorted(groups.items())]


def _mean(group: list[dict]) -> dict:
    def avg(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values)) if values else None

    last = group[-1]
    out = {"t": last["t"], "mode": last["mode"], "n": len(group)}
    for k, v in last.items():
        if k not in out and k != "w" and not isinstance(v, str):
            out[k] = avg(s.get(k) for s in group)
    out["w"] = {n: {k: avg((s.get("w") or {}).get(n, {}).get(k) for s in group) for k in fields}
                for n, fields in last.get("w", {}).items()}
    if errors := [s["gpu_error"] for s in group if "gpu_error" in s]:
        out["gpu_error"] = errors[-1]
    return out


class Dashboard:
    def __init__(self, arbiter):
        self.arbiter = arbiter
        self.telemetry = arbiter.telemetry
        self._status = (0.0, None)
        self._status_lock = threading.Lock()

    def status(self) -> dict:
        with self._status_lock:
            at, cached = self._status
            if cached is None or time.monotonic() - at > STATUS_TTL_S:
                cached = {**self.arbiter.probe(), "workloads": list(self.arbiter.workloads),
                          "labels": {n: w.label for n, w in self.arbiter.workloads.items()},
                          "gpu_health": self.arbiter.gpu_health(), "now": time.time()}
                if self.telemetry is not None:
                    cached["retention_hours"] = self.telemetry.retention_s / 3600
                    cached["interval_s"] = self.telemetry.interval_s
                    cached["oldest"] = self.telemetry.oldest()
                self._status = (time.monotonic(), cached)
            return cached

    def api(self, path: str, query: dict) -> tuple[int, object]:
        if path == "/api/status":
            return 200, self.status()
        if self.telemetry is None:
            return 404, {"error": "telemetry is off (telemetry.enabled: false in the config)"}
        now = time.time()
        try:
            since = float(query.get("since", [now - 3600])[0])
            until = float(query.get("until", [now])[0])
            points = max(10, min(5000, int(query.get("points", [800])[0])))
            limit = max(1, min(10000, int(query.get("limit", [1000])[0])))
        except ValueError:
            return 400, {"error": "since, until, points and limit are numbers"}
        if path == "/api/history":
            return 200, bucket(self.telemetry.samples(since, until), points)
        if path == "/api/events":
            return 200, self.telemetry.events(since, until, limit)
        if path == "/api/logs":
            return 200, self.telemetry.logs(since, until, limit)
        return 404, {"error": f"no such endpoint: {path}"}

    def handler(self):
        dash = self
        page = resources.files("vhotseat").joinpath("web.html").read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body: bytes, kind: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urlparse(self.path)
                if url.path in ("/", "/index.html"):
                    return self._send(200, page, "text/html; charset=utf-8")
                try:
                    code, body = dash.api(url.path, parse_qs(url.query))
                except Exception as e:  # noqa: BLE001 - report it to the page rather than drop the connection
                    code, body = 500, {"error": f"{type(e).__name__}: {e}"}
                self._send(code, json.dumps(body).encode(), "application/json")

            def log_message(self, *args):
                pass

        return Handler


def serve(arbiter, host: str, port: int) -> int:
    try:
        server = ThreadingHTTPServer((host, port), Dashboard(arbiter).handler())
    except OSError as e:
        util.log(f"can't listen on {host}:{port}: {e.strerror or e}")
        return 1
    server.daemon_threads = True
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    print(f"vhotseat web: http://{shown}:{server.server_address[1]}/", flush=True)
    if arbiter.telemetry is None:
        util.log("telemetry is off in the config: the dashboard shows the live status only")
    with Sampler(arbiter):
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return 0


def record(arbiter) -> int:
    """`vhotseat record`: sample until stopped, with no web server."""
    if arbiter.telemetry is None:
        util.log("telemetry is off in the config (telemetry.enabled: false)")
        return 2
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with Sampler(arbiter):
        stop.wait()
    return 0
