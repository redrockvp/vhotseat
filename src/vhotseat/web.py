"""`vhotseat web`: a dashboard of RAM and VRAM over time, who owned the GPU,
and the event and log history, served from telemetry.db.

The page (web.html) polls a small JSON API:

  GET /api/status                    the live status (`status --json`), plus the workloads' order and
                                     the card's model, load, temperature and power
  GET /api/why                       for each workload: could it take the card now, and what stands in
                                     the way (`why`'s dry run: it releases nothing)
  GET /api/cost?since=&until=        kWh and money over that window, per holder (needs power.price)
  GET /api/history?since=&until=     samples between two Unix times, averaged into at most `points` buckets
  GET /api/events?since=&until=      events, newest first
  GET /api/logs?since=&until=        vhotseat's log lines from every process, newest first

It only reads: nothing here switches or releases anything. While it runs it
also records samples, like `vhotseat record`.
"""

import contextlib
import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import util
from .telemetry import Sampler, cost

# Probing calls every workload's API; several open tabs share one probe a second.
STATUS_TTL_S = 1.0
# `why` probes again (busy(), reachability): share it between tabs the same way.
WHY_TTL_S = 3.0


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
        self._why = (0.0, None)
        self._why_lock = threading.Lock()

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
                claim_transitions(self.arbiter, cached.get("claims", []))
                self._status = (time.monotonic(), cached)
            return cached

    def why(self) -> dict:
        with self._why_lock:
            at, cached = self._why
            if cached is None or time.monotonic() - at > WHY_TTL_S:
                cached = {"now": time.time(), "targets": self.arbiter.why_all()}
                self._why = (time.monotonic(), cached)
            return cached

    def api(self, path: str, query: dict) -> tuple[int, object]:
        if path == "/api/status":
            return 200, self.status()
        if path == "/api/why":
            return 200, self.why()
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
        if path == "/api/cost":
            price = self.arbiter.power.get("price_per_kwh")
            if not price:
                return 404, {"error": "no power.price_per_kwh in the config"}
            report = cost(self.telemetry.samples(since, until), price)
            extra = {k: v for k, v in self.arbiter.power.items() if k != "price_per_kwh"}
            return 200, {"price": price, "window_s": until - since, **extra, **report}
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
        icon = resources.files("vhotseat").joinpath("web-icon.svg").read_bytes()

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
                if url.path in ("/favicon.svg", "/favicon.ico"):
                    return self._send(200, icon, "image/svg+xml")
                try:
                    code, body = dash.api(url.path, parse_qs(url.query))
                except Exception as e:  # noqa: BLE001 - report it to the page rather than drop the connection
                    code, body = 500, {"error": f"{type(e).__name__}: {e}"}
                self._send(code, json.dumps(body).encode(), "application/json")

            def log_message(self, *args):
                pass

        return Handler


def claim_watcher(arbiter, stop: threading.Event) -> None:
    """Look for claims on `claims_poll_s`, and act on them. This is the loop the dashboard runs
    in the background: on a box where `watch` isn't running, the dashboard is the actor, and it
    has to notice a game whether or not somebody has a tab open. Running it in both processes is
    safe: the release reads what is already in the event log and takes the GPU lock."""
    every = arbiter.claim_poll_s * util.SCALE
    while not stop.wait(every):
        try:
            claim_transitions(arbiter, arbiter.claim_hits())
        except Exception as e:                              # a bad poll must not stop the watcher
            util.log(f"claim check failed: {type(e).__name__}: {e}")


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
    stop = threading.Event()
    threading.Thread(target=claim_watcher, args=(arbiter, stop), daemon=True,
                     name="vhotseat-claims").start()
    with Sampler(arbiter):
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            server.server_close()
    return 0


def watch(arbiter, polls: int | None = None) -> int:
    """`vhotseat watch`: sample the telemetry, and when a claim starts, record it and free the
    card for it.

    Nothing depends on this process to refuse a load: every gate and guard asks who owns the
    card at the moment it decides, so a load is refused whether or not watch runs. What watch
    adds is the acting half - the release a claim asks for (`on_start: release`) - and the
    record: `claim`, `claim-end`, `claim-release` and `claim-release-waiting` in the event log,
    so the dashboard can show when the card was somebody else's, what was unloaded for it and
    what is still holding. It is also the cheapest always-on sampler, which is what
    `vhotseat autostart install --command watch` starts.

    `polls` stops it after that many polls: what the tests use, and what a scheduled
    claim log can use instead of a resident process.
    """
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError):       # not the main thread: no handlers to set
            signal.signal(sig, lambda *_: stop.set())
    if arbiter.telemetry is None:
        util.log("telemetry is off in the config: watch records claim events and no samples")
    # A game allocates within seconds of starting, so the claim check runs on its own clock
    # (`claims_poll_s`, default 2 s); the telemetry sampler keeps its own, usually slower one.
    claim_every = arbiter.claim_poll_s * util.SCALE
    with Sampler(arbiter) if arbiter.telemetry else contextlib.nullcontext():
        while polls is None or polls > 0:
            polls = None if polls is None else polls - 1
            claim_transitions(arbiter, arbiter.claim_hits())
            if polls is not None and polls <= 0:
                break
            stop.wait(claim_every)
    return 0


def claim_transitions(arbiter, hits: list[dict]) -> None:
    """Record a `claim` / `claim-end` row as the set of claims changes, and for a claim that
    wants the card (`on_start: release`) ask the workloads to let go of what they hold.

    A claim is a kind of process, not a pid: one game is twenty processes, and when one of them
    exits the other nineteen still hold the card, so that is not the card opening again. What is
    already open comes from the log (see EventLog.claims_open), not from this process's memory,
    which is what keeps `watch` and the dashboard from each doing the same thing twice."""
    events = arbiter.events
    now = _groups(h for h in hits)
    was = events.claims_open()
    for key, group in now.items():
        if key in was:
            continue
        action = group[0].get("on_start") or "release"
        events.record("claim", source="watch", pid=group[0]["pid"], name=key[0], note=key[1],
                      by=group[0]["by"], processes=len(group), on_start=action)
        util.log(f"{key[0]}" + (f" x{len(group)}" if len(group) > 1 else "") +
                 f" claims the card ({key[1]}): " +
                 ("unloading what is loaded to free it" if action == "release"
                  else "nothing new loads while it runs"))
    for key, rec in was.items():
        if key in now:
            continue
        row = rec["claim"]
        n = row.get("processes") or 1
        events.record("claim-end", source="watch", pid=row.get("pid"), name=key[0], note=key[1],
                      processes=n)
        util.log(f"{key[0]}" + (f" x{n}" if n > 1 else "") + " is gone: the card is open to loads again")
    state = events.claims_open()             # ours are in the record now
    for key, group in now.items():
        if (group[0].get("on_start") or "release") != "release":
            continue                        # `block`: the loads are the lever, as before
        release_for(arbiter, key, state.get(key) or {"released": [], "waiting": []})


def release_for(arbiter, key: tuple, rec: dict) -> None:
    """One claim's share of the release: ask only for the workloads the record doesn't already
    show as released for it. So a claim's release is one action per workload rather than the same
    unload every poll, and a workload the owner chose to load after the claim started - a typed
    `vhotseat switch` is an order - is not chased.

    Both pollers call this; the GPU lock inside arbiter.claim_release is what serialises them,
    and the release starts from what the card actually holds, so a second poller that arrives
    after the first finds nothing to release and writes nothing."""
    events = arbiter.events
    pending = {n for n in arbiter.workloads if n not in set(rec.get("released") or [])}
    if not pending:
        return
    try:
        out = arbiter.claim_release({"name": key[0], "note": key[1]})
    except Exception as e:            # a watcher that lives at logon must not die on one bad poll
        util.log(f"{key[0]} ({key[1]}): couldn't free the card - {type(e).__name__}: {e}")
        return
    if out.get("by_actor"):
        return              # another watcher is mid-release for this claim: ask again next poll
    label = lambda n: arbiter.workloads[n].label if n in arbiter.workloads else n  # noqa: E731
    if out["released"]:
        names = [r["workload"] for r in out["released"]]
        events.record("claim-release", source="watch", name=key[0], note=key[1],
                      workloads=names,
                      # What went, per workload: the handle phase 3 reloads from. Kept inside the
                      # row rather than spread over its keys, where a workload called `name` or
                      # `note` would have overwritten the row's own fields.
                      freed=[{"workload": r["workload"], "freed_mib": r["freed_mib"]}
                             for r in out["released"]],
                      freed_mib=out["freed_mib"])
        util.log(f"{key[0]} ({key[1]}) freed the card: " +
                 ", ".join(f"{label(n)} {r['freed_mib']} MiB" for n, r in
                           zip(names, out["released"])) + " unloaded, nothing closed")
    already = {w["workload"] for w in rec.get("waiting") or []}
    waiting = ([{"workload": n, "why": out["declined"]} for n in out.get("would", []) if n in pending]
               if out["declined"] else out["waiting"])
    for w in waiting:
        if w["workload"] not in pending or w["workload"] in already:
            continue                        # said once per workload per claim, then retried quietly
        events.record("claim-release-waiting", source="watch", name=key[0], note=key[1],
                      workload=w["workload"], why=w["why"])
        util.log(f"{key[0]} ({key[1]}): {label(w['workload'])} keeps its model for now - {w['why']}")


def _groups(hits):
    """Group claim hits by (process name, what claimed them), keeping their pids."""
    out: dict[tuple, list] = {}
    for h in hits:
        out.setdefault((h["name"], h["note"]), []).append(h)
    return out


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
