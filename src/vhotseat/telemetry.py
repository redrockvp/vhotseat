"""Telemetry: RAM and VRAM over time, the event log and vhotseat's own log lines,
kept in one SQLite file (~/.local/state/vhotseat/telemetry.db) for `vhotseat web`.

Every CLI process writes as it runs, so the dashboard has history even when it
starts later: each status probe is a sample (switch, hold and status probe as
they go; a hold or a waiting switch also samples every `interval_s`), every
event and every log line is a row. Between commands nothing samples, so for an
unbroken history run `vhotseat record` (or `vhotseat web`, which records too)
as a service. Samplers in several processes don't double up: each skips a tick
when another process wrote a sample less than an interval ago.

Rows older than `retention_hours` are deleted as new ones arrive, and SQLite
reuses the freed pages, so the file stays at its size for one retention window:
about 6 MiB a day at the default 5 s interval with two workloads, so ~6 MiB for
the default 24 h (measured 2026-10-18: 17,280 samples of ~305 bytes, plus 300
events and 1,000 log lines). Each further workload adds ~0.8 MiB a day; halving
the interval doubles it.

Telemetry never gets in the way of a handover: any error while writing it is
swallowed, and a write waits at most 2 s for another process's.
"""

import datetime
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (t REAL NOT NULL, mode TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS samples_t ON samples (t);
CREATE TABLE IF NOT EXISTS events (t REAL NOT NULL, kind TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_t ON events (t);
CREATE TABLE IF NOT EXISTS logs (t REAL NOT NULL, source TEXT, pid INTEGER, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS logs_t ON logs (t);
"""
TABLES = ("samples", "events", "logs")
# How a read fails when the file is locked by another process or has gone: telemetry itself swallows
# writes, so this is for whoever *asks* for a number, who decides what a missing one means. Reading
# it is optional - a decision that falls over because the history is unreadable is a bug.
READ_ERRORS = (sqlite3.Error, OSError)
# The status keys that aren't a workload's.
NOT_WORKLOADS = {"mode", "gpu", "ram", "holders", "claims", "claims_state", "last_event",
                 "policy", "pressure"}


def compact(status: dict, health: dict | None = None) -> dict:
    """The numbers of a status probe worth keeping, per sample, plus the card's
    load, temperature and power (its model goes in the status, not every sample)."""
    g, r = status["gpu"], status["ram"]
    names = [n for n in status if n not in NOT_WORKLOADS]
    out = {"vram_total": g.get("total_mib"), "vram_free": g.get("free_mib"), "vram_other": g.get("other_mib"),
           "vram_shared": g.get("shared_mib"),
           "ram_total": r.get("total_mib"), "ram_available": r.get("available_mib"),
           "swap_total": r.get("swap_total_mib"), "swap_used": r.get("swap_used_mib"),
           "w": {n: {"vram": g.get(f"{n}_mib"), "ram": (r.get(n) or {}).get("ram_mib"),
                     "swap": (r.get(n) or {}).get("swap_mib")} for n in names}}
    if health:
        out.update({f"gpu_{k}": health.get(k) for k in ("util_pct", "temp_c", "power_w", "power_limit_w")})
    if "error" in g:
        out["gpu_error"] = g["error"]
    return out


def cost(samples: list[dict], price_per_kwh: float, step_s: float | None = None) -> dict:
    """What the card's sampled wattage cost over a window, and who was holding it.

    Each gap between two samples that both carry a wattage is integrated (their mean
    over that gap) and charged to whoever held the card at the start of it. What counts
    as a hole is judged against the cadence the history is kept at - callers pass it as
    `step_s`, because it is their own sampler's `interval_s` - falling back on the
    samples' median spacing when nobody says, because a stretch with no samples is no
    data, not a card that drew nothing; gaps over six steps are skipped. The median is
    the fallback, not the rule: ask it of a window holding three samples an hour apart
    and it answers "this history is kept hourly", and the outage becomes an hour of
    watts. What this counts is the card's own draw as the driver reports it:
    the rest of the machine, and the PSU burning some of it on the way in, are not in
    the number - so treat it as a floor, not the line on your bill."""
    out: dict[str, dict] = {}
    total = 0.0
    gaps = sorted(g for g in (b["t"] - a["t"] for a, b in zip(samples, samples[1:])) if g > 0)
    # Six steps of the cadence the history is kept at, whoever read it last saying what that is:
    # the median spacing is the fallback, and on a window with three samples in it the fallback is
    # the outage itself.
    limit = 6 * max(step_s or (gaps[len(gaps) // 2] if gaps else 1.0), 1.0)
    for a, b in zip(samples, samples[1:]):
        w1, w2, dt = a.get("gpu_power_w"), b.get("gpu_power_w"), b["t"] - a["t"]
        if w1 is None or w2 is None or dt <= 0 or dt > limit:
            continue
        kwh = (w1 + w2) / 2 * dt / 3_600_000      # W x s -> kWh
        cell = out.setdefault(a.get("mode") or "none", {"kwh": 0.0, "hours": 0.0})
        cell["kwh"] += kwh
        cell["hours"] += dt / 3600
        total += kwh
    return {"kwh": round(total, 3), "cost": round(total * price_per_kwh, 2),
            "hours": round(sum(c["hours"] for c in out.values()), 2),
            "w_now": (samples[-1].get("gpu_power_w") if samples else None),
            "by_mode": {m: {"kwh": round(c["kwh"], 3), "cost": round(c["kwh"] * price_per_kwh, 2),
                            "hours": round(c["hours"], 2)} for m, c in out.items()}}


class Telemetry:
    def __init__(self, path: Path, retention_hours: float = 24, interval_s: float = 5,
                 seed_events: Path | None = None):
        self.path = path
        # The event log to copy in when the database is new, so the history starts with it.
        self.seed_events = seed_events
        self.retention_s = retention_hours * 3600
        self.interval_s = interval_s
        # What this process is doing ("switch comfy"), stored with its log lines.
        self.source = ""
        self._db = None
        self._mutex = threading.Lock()
        self._last_sample = 0.0
        self._last_mode = None
        self._last_prune = 0.0

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists()
            db = sqlite3.connect(self.path, timeout=2, check_same_thread=False, isolation_level=None)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(SCHEMA)
            if new and self.seed_events is not None:
                db.executemany("INSERT INTO events VALUES (?, ?, ?)", self._seed())
            self._db = db
        return self._db

    def _seed(self):
        try:
            lines = self.seed_events.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        oldest = time.time() - self.retention_s
        for line in lines:
            try:
                event = json.loads(line)
                t = datetime.datetime.fromisoformat(event["time"]).timestamp()
            except (ValueError, KeyError, TypeError):
                continue
            if t >= oldest:
                yield t, event.get("event"), json.dumps(event, separators=(",", ":"))

    def _write(self, sql: str, args: tuple) -> None:
        try:
            with self._mutex:
                db = self._conn()
                db.execute(sql, args)
                now = time.time()
                if now - self._last_prune > 60:
                    self._last_prune = now
                    for table in TABLES:
                        db.execute(f"DELETE FROM {table} WHERE t < ?", (now - self.retention_s,))
        except READ_ERRORS:
            pass

    def _read(self, sql: str, args: tuple) -> list:
        with self._mutex:
            return self._conn().execute(sql, args).fetchall()

    # --- writing ---------------------------------------------------------------

    def sample(self, status: dict, health=None) -> None:
        """Keep a status probe: at most one a second per process, unless the GPU
        changed hands (a switch's last probe is the one that shows it did)."""
        now = time.time()
        if now - self._last_sample < 1 and status.get("mode") == self._last_mode:
            return
        self._last_sample, self._last_mode = now, status.get("mode")
        # `health` is a callable: read only when the sample is kept.
        self._write("INSERT INTO samples VALUES (?, ?, ?)",
                    (now, status.get("mode"), json.dumps(compact(status, health() if health else None),
                                                         separators=(",", ":"))))

    def event(self, event: dict) -> None:
        self._write("INSERT INTO events VALUES (?, ?, ?)",
                    (time.time(), event.get("event"), json.dumps(event, separators=(",", ":"))))

    def log(self, message: str) -> None:
        self._write("INSERT INTO logs VALUES (?, ?, ?, ?)", (time.time(), self.source, os.getpid(), message))

    def due(self) -> bool:
        """Whether a sampler should take a sample now: no process wrote one for an interval."""
        try:
            last = self._read("SELECT max(t) FROM samples", ())[0][0] or 0
        except READ_ERRORS:
            last = 0
        return time.time() - max(last, self._last_sample) >= self.interval_s * 0.8

    # --- reading ---------------------------------------------------------------

    def samples(self, since: float, until: float) -> list[dict]:
        rows = self._read("SELECT t, mode, data FROM samples WHERE t >= ? AND t <= ? ORDER BY t", (since, until))
        return [{"t": t, "mode": mode, **json.loads(data)} for t, mode, data in rows]

    def events(self, since: float, until: float, limit: int = 1000) -> list[dict]:
        rows = self._read("SELECT t, data FROM events WHERE t >= ? AND t <= ? ORDER BY t DESC LIMIT ?",
                          (since, until, limit))
        return [{"t": t, **json.loads(data)} for t, data in rows]

    def logs(self, since: float, until: float, limit: int = 1000) -> list[dict]:
        rows = self._read("SELECT t, source, pid, message FROM logs WHERE t >= ? AND t <= ? "
                          "ORDER BY t DESC LIMIT ?", (since, until, limit))
        return [{"t": t, "source": s, "pid": p, "message": m} for t, s, p, m in rows]

    def peaks(self, names: list[str]) -> dict:
        """The most VRAM each workload has been recorded holding over the retention window.

        This is what "would these two fit on the card together?" has to be measured against: a
        workload at its worst, because that is what it costs when it is loaded. Measured against
        what the card happens to be doing this second, the answer is a snapshot of luck. Read with
        json_extract so the samples don't have to be pulled into Python for one number. What the
        processes vhotseat doesn't manage hold is deliberately not here as a peak too: that number
        is a floor, not a maximum, and `floor_mib()` is where it lives.
        """
        cols, args = [], []
        for n in names:
            cols.append("max(cast(json_extract(data, ?) as integer))")
            args.append(f"$.w.{n}.vram")
        try:
            row = self._read(f"SELECT {', '.join(cols)} FROM samples WHERE t >= ?",
                             (*args, time.time() - self.retention_s))[0]
        except (sqlite3.Error, IndexError, OSError):
            return {}
        return dict(zip(names, row, strict=True))

    def floor_mib(self) -> int | None:
        """The non-workload VRAM this card *usually* carries: dwm, the browser, Orca, an overlay.

        The median of `vram_other` over the retention window, and the median is the whole point.
        The peak is the wrong number: a game, Blender, or whatever a claim rule watches all count
        as `other` while they run, and all of their memory comes back when they exit. Read a peak
        over a day that had one evening of gaming in it and every pair of workloads says "no room"
        for the next 24 hours, and a trigger built on the same number would spend a handover
        chasing gigabytes that were never there. A median ignores that spike - an evening is a
        minority of the samples - and stays low, so what it describes is the desktop doing
        something ordinary.

        One function because two answers to this question would be a bug: the dashboard's "fits
        alongside" and the pressure trigger's idea of how much of the card can never be freed have
        to be the same number.

        None when the window holds no such sample; whoever asks decides what to fall back on.
        """
        rows = self._read("SELECT cast(json_extract(data, '$.vram_other') as integer) FROM samples "
                          "WHERE t >= ? AND json_extract(data, '$.vram_other') is not null",
                          (time.time() - self.retention_s,))
        values = sorted(r[0] for r in rows if r[0] is not None)
        if not values:
            return None
        mid = len(values) // 2
        return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) // 2

    def below_free(self, mib: int, within_s: float) -> tuple[float, int] | None:
        """How long the card has been continuously under `mib` MiB free, from the samples:
        (start of the run, how many samples in it). None when the newest sample isn't under it,
        or when there are no samples at all.

        "Has the card been short for 30 s" is read from the samples rather than from a counter in
        the process asking, for the usual reason: `watch` and the dashboard both ask, and neither
        may hold private state about what the other already decided. It is also why the trigger
        needs a sampler running - with no samples the answer is "I don't know", and not knowing
        does not unload a 20 GB model.

        A sample with nothing to say about free VRAM (a card read that failed) ends the run: a
        stretch that wasn't measured is not a stretch that was short.
        """
        rows = self._read("SELECT t, cast(json_extract(data, '$.vram_free') as integer) FROM samples "
                          "WHERE t >= ? ORDER BY t DESC LIMIT 400", (time.time() - within_s,))
        start, count = None, 0
        for t, free in rows:
            if free is None or free >= mib:
                break
            start, count = t, count + 1
        return (start, count) if start is not None else None

    def oldest(self) -> float | None:
        return self._read("SELECT min(t) FROM samples", ())[0][0]


class Sampler:
    """Probe every `interval_s` in a background thread, while no other process does."""

    def __init__(self, arbiter):
        self.arbiter = arbiter
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="vhotseat-sampler", daemon=True)

    def _run(self) -> None:
        tel = self.arbiter.telemetry
        while True:
            started = time.monotonic()
            if tel.due():
                try:
                    self.arbiter.probe()
                except Exception:  # noqa: BLE001 - a failed probe mustn't end the sampling
                    pass
            # A probe takes seconds (systemctl, every workload's API): keep to the
            # interval rather than adding it after each probe.
            if self._stop.wait(max(0.5, tel.interval_s - (time.monotonic() - started))):
                return

    def __enter__(self):
        if self.arbiter.telemetry is not None:
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
