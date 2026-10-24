"""`vhotseat web`: a dashboard of RAM and VRAM over time, who owned the GPU,
and the event and log history, served from telemetry.db.

The page (web.html) polls a small JSON API:

  GET /api/status                    the live status (`status --json`), plus the workloads' order and
                                     the card's model, load, temperature and power, the policy in
                                     force (read-only), what each workload has peaked at, which
                                     pairs of them fit on the card together, and the recent policy
                                     warnings the page should show
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

from . import policy, pressure, util
from .telemetry import READ_ERRORS, Sampler, cost

# Probing calls every workload's API; several open tabs share one probe a second.
STATUS_TTL_S = 1.0
# `why` probes again (busy(), reachability): share it between tabs the same way.
WHY_TTL_S = 3.0
# The event rows that are a warning rather than a record: a load a policy refused, and a
# workload a claim was not allowed to unload. Both say somebody may be short of VRAM.
POLICY_WARNING_ROWS = ("policy-refused", "policy-kept")
# How recent one has to be to still be shown on the page: long enough that the next look at
# the dashboard sees it, short enough that a refusal from this morning isn't shouting at you
# this afternoon. It's the row's own timestamp that decides, so every process serving a page
# sees the same set.
WARNING_AGE_S = 300.0


def fits_on_card(peaks: dict, floor: int, idle: dict, total_mib: int | None) -> list[dict]:
    """For each pair of workloads: would they fit on the card at the same time?

    Each one at the most it has been *recorded* holding - a workload costs what its biggest
    model costs, not what it happens to be holding this second - plus `floor`: what the
    processes vhotseat doesn't manage *usually* hold, which is the part of the card no handover
    frees. `floor` comes from Telemetry.floor_mib(), median not peak, and that difference is the
    whole correction: what a game or Blender holds comes back when they close, so counting their
    worst moment as permanent would have said "no room" for a day after one evening of gaming.

    `fits` is null for a pair where one of the two has never been sampled holding more than
    its idle weight: the honest answer there is that the card has never been seen doing it,
    not that it would have fitted."""
    if total_mib is None:
        return []
    names = list(peaks)
    rows = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            never = [n for n in (a, b) if (peaks.get(n) or 0) <= (idle.get(n) or 0)]
            if never:
                rows.append({"a": a, "b": b, "mib": None, "fits": None,
                             "why": "never recorded holding models: " + ", ".join(never)})
                continue
            mib = peaks[a] + peaks[b] + floor
            rows.append({"a": a, "b": b, "mib": mib, "fits": mib <= total_mib, "why": None})
    return rows


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
                          "idle": {n: w.idle_vram_mib for n, w in self.arbiter.workloads.items()},
                          "gpu_health": self.arbiter.gpu_health(), "now": time.time()}
                if self.telemetry is not None:
                    cached["retention_hours"] = self.telemetry.retention_s / 3600
                    cached["interval_s"] = self.telemetry.interval_s
                    oldest, oldest_err = self._history_read("oldest")
                    cached["oldest"] = oldest
                else:
                    oldest_err = None
                watch_pass(self.arbiter, cached.get("claims", []))
                if "claims_state" in cached:
                    # What has been unloaded for an open claim, and what is still holding. The
                    # log rebuilds it keyed by (process name, what claimed it) because that is
                    # what `status` and `claims` unpack, and a JSON object has no tuple keys: an
                    # open claim used to kill the response in json.dumps and leave the page with
                    # a closed connection. Rows with the name and note in them are what a page
                    # wants anyway.
                    cached["claims_state"] = [{**rec, "name": key[0], "note": key[1]}
                                              for key, rec in cached["claims_state"].items()]
                # The policy is the config's, shown here read-only until there's a way to edit
                # it in the page; the warnings come from the event log, which is the shared
                # state, so a refusal written by `watch` warns here too.
                cached["policy_warnings"] = self.arbiter.events.recent(POLICY_WARNING_ROWS,
                                                                      within_s=WARNING_AGE_S)
                if cached.get("pressure"):
                    # The page says "try 2 of 3" for a workload whose unload doesn't take, so it
                    # needs the number the watcher is actually counting to, not one copied here.
                    cached["pressure"]["stuck_tries"] = STUCK_TRIES
                peaks, peaks_err = self._peaks(cached["gpu"])
                floor, floor_err = self._floor(cached["gpu"])
                cached["peaks"] = peaks
                cached["floor"] = floor
                cached["fits"] = fits_on_card(peaks, floor, {n: w.idle_vram_mib
                                                             for n, w in self.arbiter.workloads.items()},
                                              cached["gpu"].get("total_mib"))
                if unreadable := next((e for e in (oldest_err, peaks_err, floor_err) if e), None):
                    # Three of the page's numbers come from the history rather than the card, and a
                    # file that couldn't be opened looks exactly like a box with no samples in it yet:
                    # no "history since" line, and a fit worked out from this second's reading alone.
                    # Only one of the two is something the owner can fix, so the page says which.
                    cached["history_error"] = unreadable
                self._status = (time.monotonic(), cached)
            return cached

    def _history_read(self, method: str, *args) -> tuple:
        """One number out of the telemetry, or (None, why it isn't there). The history is what the
        page draws pictures from, never what the card is decided on: a database another process has
        locked, or one that has gone corrupt, may cost the page its history line, but the live card,
        the holders, the open claim and the pressure episode underneath it all still answer. That is
        the same rule the arbiter applies to the pressure readings."""
        if self.telemetry is None:
            return None, None
        try:
            return getattr(self.telemetry, method)(*args), None
        except READ_ERRORS as e:
            return None, str(e) or type(e).__name__

    def _peaks(self, gpu: dict) -> tuple[dict, str | None]:
        """The most VRAM each workload has been seen holding: what telemetry recorded over the
        retention window, floored by what the card shows right now - a 14 GB model sitting on the
        card today has cost at least that much, whatever the sampled window happened to catch. When
        nothing was recorded, the card's own reading is the whole answer, which is the fit at its
        most optimistic: `history_error` is what says whether that is a fresh box or a file that
        couldn't be read."""
        recorded, err = self._history_read("peaks", list(self.arbiter.workloads))
        recorded = recorded or {}

        def highest(*values):
            return max((v for v in values if v is not None), default=None)

        return ({n: highest(recorded.get(n), gpu.get(f"{n}_mib")) for n in self.arbiter.workloads},
                err)

    def _floor(self, gpu: dict) -> tuple[int, str | None]:
        """What the processes vhotseat doesn't manage usually hold on this card. The median over
        the telemetry window; the current reading when there are no samples yet, or none can be
        read, which is the best available the first time anything asks. Not the peak: see
        Telemetry.floor_mib()."""
        recorded, err = self._history_read("floor_mib")
        return (recorded if recorded is not None else (gpu.get("other_mib") or 0)), err

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
            # The cadence this history was kept at, so an hour nobody sampled is a hole in the
            # range rather than an hour the card drew its last reading for: `hours` then says what
            # was sampled, which is what the page's "this is a floor" note is measured against.
            report = cost(self.telemetry.samples(since, until), price,
                          step_s=self.telemetry.interval_s)
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
            watch_pass(arbiter, arbiter.claim_hits())
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
            watch_pass(arbiter, arbiter.claim_hits())
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


CLAIM_STUCK_TRIES = 3      # tries at a workload that says it unloaded and keeps holding


def release_for(arbiter, key: tuple, rec: dict) -> None:
    """One claim's share of the release.

    The workloads it is owed are decided once, when its release first runs, from what the card
    held at that moment, and recorded. So a model the owner loads in the middle of a game - a
    typed `vhotseat switch` is an order - stays theirs, and no later poll unloads it; and a
    workload that held nothing when the claim started isn't something the claim is waiting on,
    and neither is one whose `yields_to:` doesn't cover a claim: that one is recorded as kept
    and left holding, because the game is short of it and nothing here is owed it.
    Once every target is released the claim has been given its due: later polls take no GPU
    lock and read no card for it, for as long as the game runs.

    A target that is working is asked again on every poll while the claim lasts. A target that
    says it unloaded and keeps holding its VRAM is asked CLAIM_STUCK_TRIES times in total: that
    is a unload that doesn't work, not a busy moment, and asking forever would spend the whole
    game taking and releasing the GPU lock for nothing. The row for the last try says so."""
    events = arbiter.events
    if rec.get("targets") is None:
        targets = arbiter.claim_targets()
        # The workloads that are on the card and are *not* owed to this claim, because their
        # `yields_to:` doesn't cover a claim. A release is never refused, so the claim simply
        # goes ahead without them; the game may be short of exactly that much memory, so each
        # one gets a row of its own and a line on the dashboard. Decided here, once, which is
        # also what keeps a long game from writing a warning every 2 s.
        kept = arbiter.kept_for_policy(policy.CLAIMS)
        events.record("claim-release-targets", source="watch", name=key[0], note=key[1],
                      targets=targets, kept=[k["workload"] for k in kept])
        rec = {**rec, "targets": targets}
        for k in kept:
            why = arbiter.kept_why(policy.CLAIMS, k["workload"], k["held_mib"])
            events.record("policy-kept", source="watch", name=key[0], note=key[1],
                          workload=k["workload"], held_mib=k["held_mib"], why=why)
            util.log(f"{key[0]} ({key[1]}): {why}")
    label = lambda n: arbiter.workloads[n].label if n in arbiter.workloads else n  # noqa: E731
    waiting = rec.get("waiting") or []
    stuck = {w["workload"]: w.get("attempts") or 1 for w in waiting if w.get("stuck")}
    held = {w["workload"]: w.get("held_mib") or 0 for w in waiting}
    given = set(rec.get("released") or [])
    pending = [n for n in rec["targets"] if n not in given and stuck.get(n, 0) < CLAIM_STUCK_TRIES]
    if not pending:
        return
    # Only now is the card read again, and only while something is still owed. A target that
    # has come clear by itself - the unload landing late, or the owner closing the program -
    # is as done as the claim cares: say so, so the log and `status` stop naming it as holding.
    snap = arbiter.snapshot()
    clear = [n for n in pending
             if n not in arbiter.workloads
             or (snap.get(f"{n}_mib") or 0) <= arbiter.workloads[n].idle_vram_mib]
    still = [n for n in pending if n not in clear]
    if clear:
        # What it was last seen holding, which is what stopped being held. Zero when there was
        # nothing to measure - the owner closed the program rather than releasing it.
        events.record("claim-release", source="watch", name=key[0], note=key[1],
                      workloads=clear, freed=[{"workload": n, "freed_mib": held.get(n, 0)}
                                              for n in clear if held.get(n, 0)],
                      freed_mib=sum(held.get(n, 0) for n in clear), late=True)
        util.log(f"{key[0]} ({key[1]}): " + ", ".join(label(n) for n in clear) +
                 " holds nothing on the card now")
    if not still:
        return
    try:
        out = arbiter.claim_release({"name": key[0], "note": key[1]}, only=still)
    except Exception as e:            # a watcher that lives at logon must not die on one bad poll
        util.log(f"{key[0]} ({key[1]}): couldn't free the card - {type(e).__name__}: {e}")
        return
    if out.get("by_actor"):
        return          # another watcher is mid-release for this claim: ask again on the next poll
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
                 ", ".join(f"{label(r['workload'])} {r['freed_mib']} MiB" for r in out["released"]) +
                 " unloaded, nothing closed")
    if out["declined"] is not None:
        asked = [{"workload": n, "why": out["declined"]} for n in out.get("would") or []]
    else:
        asked = out["waiting"]
    said = {w["workload"] for w in waiting}
    for w in asked:
        n = w["workload"]
        if n not in still:
            continue
        if w.get("stuck"):
            k = stuck.get(n, 0) + 1
            why = w["why"] + (f" - {k} tries and it still holds, so leaving it until the claim ends"
                              if k >= CLAIM_STUCK_TRIES else "")
            events.record("claim-release-waiting", source="watch", name=key[0], note=key[1],
                          workload=n, why=why, attempts=k, stuck=True, held_mib=w.get("held_mib") or 0)
            util.log(f"{key[0]} ({key[1]}): {label(n)} keeps its model - {why}")
        elif n not in said:
            events.record("claim-release-waiting", source="watch", name=key[0], note=key[1],
                          workload=n, why=w["why"])
            util.log(f"{key[0]} ({key[1]}): {label(n)} keeps its model for now - {w['why']}")


def _groups(hits):
    """Group claim hits by (process name, what claimed them), keeping their pids."""
    out: dict[tuple, list] = {}
    for h in hits:
        out.setdefault((h["name"], h["note"]), []).append(h)
    return out


# How many tries a workload that says it unloaded and keeps holding its VRAM gets in one episode,
# before it is left alone. That is an unload that doesn't work, not a busy moment, and asking every
# poll for as long as an app holds the card would spend the whole time taking the GPU lock for
# nothing. The same patience phase 1 uses for a claim.
STUCK_TRIES = 3


def watch_pass(arbiter, hits: list[dict]) -> None:
    """One pass of what the watcher does: record the claims and act on them, then look at the
    pressure the claims don't explain. One call because both halves want the same process scan, and
    both are safe to run from more than one process on the box."""
    claim_transitions(arbiter, hits)
    pressure_pass(arbiter, {h["pid"] for h in hits})


def pressure_pass(arbiter, claimed: set = frozenset()) -> None:
    """One look at the pressure trigger: end an episode that is over, keep an open one going, or
    start one if the card says it should.

    `claimed` are the pids the claims from this same poll cover: a game is phase 1's, already handed
    the card by its own rule, so its memory is not also a reason to open an episode over it. Whether
    an episode is open, which workloads it was owed, and which have come free are read back from the
    event log, which is what makes this safe for `watch`, the dashboard's watcher and a status
    request to all run on one box: the second one finds the episode, not a card to free again.

    Nothing is logged when the numbers simply aren't pressure - that is every poll of an ordinary
    day, and a watcher that printed its reasoning twice a second would bury the lines that matter.
    What is logged is what moved: an episode opening, a model coming off, a workload still holding,
    and the episode ending.
    """
    events = arbiter.events
    label = lambda n: arbiter.workloads[n].label if n in arbiter.workloads else n  # noqa: E731
    ep = events.episode_open()
    if ep is None:
        out = arbiter.pressure_start(claimed=claimed)
        if out["released"]:
            util.log("pressure: " + pressure.describe(out.get("episode") or {}) + " - " +
                     ", ".join(f"{label(r['workload'])} {r['freed_mib']} MiB"
                                for r in out["released"]) + " unloaded, nothing closed")
        return

    reason = arbiter.pressure_over(ep)
    if reason:
        events.record("pressure-end", source="watch", name=ep["episode"].get("name"),
                      reason=reason, freed_mib=ep.get("freed_mib") or 0)
        util.log(f"pressure: {reason}: the card is open to loads again")
        return

    waiting = {w["workload"]: w for w in ep.get("waiting") or []}
    # A stuck one gets STUCK_TRIES in total, counted from the rows so both pollers are counting the
    # same three. A busy one is asked again on every poll for as long as the episode lasts: it is
    # working, and work in flight is never interrupted for an app nobody named.
    tired = {n for n, w in waiting.items()
             if w.get("stuck") and (w.get("attempts") or 1) >= STUCK_TRIES}
    if any((waiting[n].get("attempts") or 1) == STUCK_TRIES for n in tired):
        util.log(f"pressure: leaving {', '.join(sorted(tired))} alone: "
                 f"{STUCK_TRIES} tries and it still holds its VRAM")
    owed = (set(ep.get("targets") or []) - set(ep.get("released") or [])) - tired
    if not owed:
        return
    out = arbiter.pressure_release(ep)
    if out["released"]:
        util.log("pressure: " + ", ".join(f"{label(r['workload'])} {r['freed_mib']} MiB"
                                          for r in out["released"]) + " unloaded, nothing closed")
    for w in out["waiting"]:
        if not w.get("stuck"):
            util.log(f"pressure: {label(w['workload'])} keeps its model for now - {w['why']}")


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
