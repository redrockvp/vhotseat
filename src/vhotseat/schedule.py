"""GPU scheduling: priority classes, bookings and tickets.

Without a `schedule:` block in the config there is no schedule: every gate, guard and switch
behaves exactly as it always has, and the schedule commands say there is nothing to show.

With one, the card can be **booked**: a time window that holds it for its owner. A booking's
class says whose window it is:

- `peter` - the owner's own time ("Peter's day", a quiet block). Inside it only requests of
  class `peter` are admitted: the ComfyUI web page, a `switch`/`hold` someone typed, a game
  (games are claims, and claims were never gated). Nothing new loads for an agent.
- `booked` - a planned run ("SchoolMap extraction 22:00-01:00") for the workloads it names.
  Inside it, requests for those workloads are admitted (and requests of class `peter`,
  always); anything else waits.

A booking is about **new** loads only. Work already running finishes - vhotseat never
interrupts a render or an answer - and a load that a `switch` or `hold` typed by a person asked
for goes through as an order (recorded as `schedule-override`), like everywhere else.

A **ticket** is one queued request (requester, class, workload, estimate, pid). The table is
kept here; `vhotseat run` and `hold --class` fill it (see docs/reference.md). A ticket is alive
only while its process is, so a crashed caller never stays at the head of the queue.

The store is a small SQLite file beside the event log (`schedule.db`). Times are epoch seconds;
recurrence keeps the local wall-clock time of the first window (`daily`, `weekdays`).
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import sqlite3
import time
from pathlib import Path

DEFAULT_CLASSES = [
    {"name": "peter"},
    {"name": "booked"},
    {"name": "agent", "max_hold_s": 900},
    {"name": "batch", "max_hold_s": 1200},
    {"name": "background", "max_hold_s": 600},
]
KEYS = ("classes", "default_class", "affinity_s")
BOOKING_CLASSES = ("peter", "booked")
EVERY = ("daily", "weekdays")
MODES = ("drain", "hard")
DAY = 86400.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS booking (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner TEXT NOT NULL,
  class TEXT NOT NULL,
  workloads TEXT NOT NULL,            -- JSON list of workload names, or ["*"]
  start REAL NOT NULL,                -- first window, epoch seconds
  end REAL NOT NULL,
  every TEXT,                         -- NULL, 'daily' or 'weekdays'
  mode TEXT NOT NULL DEFAULT 'drain',
  source TEXT NOT NULL DEFAULT 'local',
  note TEXT NOT NULL DEFAULT '',
  paused_until REAL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  requester TEXT NOT NULL,
  class TEXT NOT NULL,
  workload TEXT NOT NULL,
  model TEXT,
  est_s REAL,
  pid INTEGER,
  state TEXT NOT NULL,                -- waiting | running | yielded | done | expired
  created REAL NOT NULL,
  heartbeat REAL NOT NULL,
  started REAL,
  ended REAL,
  note TEXT NOT NULL DEFAULT ''
);
"""


def cfg(raw: dict | None) -> dict | None:
    """The `schedule:` block with its defaults, or None when the config has none."""
    if raw is None:
        return None
    classes = [dict(c) for c in (raw.get("classes") or DEFAULT_CLASSES)]
    return {"classes": classes, "default_class": dict(raw.get("default_class") or {}),
            "affinity_s": float(raw.get("affinity_s", 600))}


def clock(t: float) -> str:
    """A window edge as people read it: 22:00 today, or a date when it isn't today."""
    d = datetime.datetime.fromtimestamp(t)
    if d.date() == datetime.date.today():
        return d.strftime("%H:%M")
    return d.strftime("%a %d %b %H:%M")


def parse_time(text: str, now: float) -> float:
    """HH:MM (today, local), or an ISO date-time ("2026-11-17 22:00", "2026-11-17T22:00")."""
    text = text.strip()
    try:
        hh, mm = text.split(":")
        if len(hh) <= 2 and len(mm) == 2:
            h, m = int(hh), int(mm)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            base = datetime.datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0, microsecond=0)
            return base.timestamp()
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(text.replace(" ", "T")).timestamp()
    except ValueError:
        raise ValueError(f"not a time: {text!r} (HH:MM, or YYYY-MM-DD HH:MM)") from None


def _shift_days(t: float, days: int) -> float:
    """The same local wall-clock time `days` later (so a daily 08:00 stays 08:00 across DST)."""
    return (datetime.datetime.fromtimestamp(t) + datetime.timedelta(days=days)).timestamp()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ScheduleError(Exception):
    pass


class Schedule:
    def __init__(self, path: Path, conf: dict, *, now=time.time, alive=_pid_alive):
        self.path = Path(path)
        self.conf = conf
        self.now = now
        self.alive = alive
        self.classes = [c["name"] for c in conf["classes"]]
        self.default_class = conf["default_class"]

    # --- store ------------------------------------------------------------------------------
    @contextlib.contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=10)
        try:
            con.row_factory = sqlite3.Row
            con.executescript(_SCHEMA)
            with con:
                yield con
        finally:
            con.close()

    # --- classes ----------------------------------------------------------------------------
    def rank(self, cls: str) -> int:
        """0 is the highest class."""
        return self.classes.index(cls) if cls in self.classes else len(self.classes)

    def class_of(self, workload: str) -> str:
        """The class of a request nobody tagged (a gate's load, a guard's job)."""
        return self.default_class.get(workload, "agent")

    def max_hold_s(self, cls: str) -> float | None:
        for c in self.conf["classes"]:
            if c["name"] == cls:
                return c.get("max_hold_s")
        return None

    # --- bookings ---------------------------------------------------------------------------
    def add_booking(self, owner: str, start: float, end: float, *, cls: str = "booked",
                    workloads=("*",), every: str | None = None, mode: str = "drain",
                    source: str = "local", note: str = "") -> dict:
        if cls not in BOOKING_CLASSES:
            raise ScheduleError(f"a booking's class is {' or '.join(BOOKING_CLASSES)}, not {cls!r}")
        if every not in (None, *EVERY):
            raise ScheduleError(f"--every is {' or '.join(EVERY)}, not {every!r}")
        if mode not in MODES:
            raise ScheduleError(f"--mode is {' or '.join(MODES)}, not {mode!r}")
        if end <= start:
            raise ScheduleError("a booking has to end after it starts")
        if every and end - start > DAY:
            raise ScheduleError("a recurring booking can't be longer than a day")
        workloads = list(workloads) or ["*"]
        if cls == "booked":
            # A planned run can't be put over the owner's own time: say which window is in the way.
            for b in self.bookings():
                if b["class"] != "peter":
                    continue
                for s, e in self.windows(b, start, end if not every else start + 14 * DAY):
                    if self._overlaps_series(start, end, every, s, e):
                        raise ScheduleError(f"it overlaps booking {b['id']} ({b['owner']}, {clock(s)}-{clock(e)}), "
                                            "which is the owner's time")
        row = (owner, cls, json.dumps(workloads), start, end, every, mode, source, note, self.now())
        with self._db() as con:
            cur = con.execute("INSERT INTO booking (owner, class, workloads, start, end, every, mode, source, "
                              "note, created) VALUES (?,?,?,?,?,?,?,?,?,?)", row)
            bid = cur.lastrowid
        return self.booking(bid)

    def _overlaps_series(self, start, end, every, s, e) -> bool:
        if not every:
            return start < e and s < end
        k = 0
        while (a := _shift_days(start, k)) < e:
            b = _shift_days(end, k)
            if (every != "weekdays" or datetime.datetime.fromtimestamp(a).weekday() < 5) and a < e and s < b:
                return True
            k += 1
            if k > 15:
                break
        return False

    def booking(self, bid: int) -> dict | None:
        with self._db() as con:
            r = con.execute("SELECT * FROM booking WHERE id = ?", (bid,)).fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["workloads"] = json.loads(d["workloads"])
        return d

    def bookings(self) -> list[dict]:
        with self._db() as con:
            return [self._row(r) for r in con.execute("SELECT * FROM booking ORDER BY start, id")]

    def remove_booking(self, bid: int) -> dict:
        b = self.booking(bid)
        if b is None:
            raise ScheduleError(f"no booking {bid}")
        with self._db() as con:
            con.execute("DELETE FROM booking WHERE id = ?", (bid,))
        return b

    def pause(self, bid: int, until: float | None = None) -> dict:
        """Switch a booking off until `until` (default: the end of the window it's in, or of its
        next one) - "the GPU is free today" without deleting the recurring booking."""
        b = self.booking(bid)
        if b is None:
            raise ScheduleError(f"no booking {bid}")
        now = self.now()
        if until is None:
            nxt = next(iter(self.windows(b, now, now + 8 * DAY, raw=True)), None)
            if nxt is None:
                raise ScheduleError(f"booking {bid} has no window left to pause")
            until = nxt[1]
        with self._db() as con:
            con.execute("UPDATE booking SET paused_until = ? WHERE id = ?", (until, bid))
        return self.booking(bid)

    def resume(self, bid: int) -> dict:
        if self.booking(bid) is None:
            raise ScheduleError(f"no booking {bid}")
        with self._db() as con:
            con.execute("UPDATE booking SET paused_until = NULL WHERE id = ?", (bid,))
        return self.booking(bid)

    def windows(self, b: dict, frm: float, to: float, raw: bool = False) -> list[tuple[float, float]]:
        """The booking's windows that overlap [frm, to), in order. Unless `raw`, a paused stretch
        is cut out (a window entirely inside the pause is skipped)."""
        out = []
        if not b["every"]:
            cands = [(b["start"], b["end"])]
        else:
            cands = []
            k = max(0, int((frm - b["start"]) // DAY) - 1)
            while True:
                s, e = _shift_days(b["start"], k), _shift_days(b["end"], k)
                if s >= to:
                    break
                if b["every"] != "weekdays" or datetime.datetime.fromtimestamp(s).weekday() < 5:
                    cands.append((s, e))
                k += 1
                if k > 3660:
                    break
        for s, e in cands:
            if not raw and b.get("paused_until"):
                s = max(s, b["paused_until"])
            if s < e and s < to and e > frm:
                out.append((s, e))
        return out

    def active(self, now: float | None = None) -> list[tuple[dict, float, float]]:
        now = self.now() if now is None else now
        return [(b, s, e) for b in self.bookings() for s, e in self.windows(b, now, now + 1) if s <= now < e]

    def upcoming(self, within_s: float, now: float | None = None) -> list[tuple[dict, float, float]]:
        now = self.now() if now is None else now
        out = [(b, s, e) for b in self.bookings() for s, e in self.windows(b, now, now + within_s) if s > now]
        return sorted(out, key=lambda x: x[1])

    # --- admission --------------------------------------------------------------------------
    def excludes(self, b: dict, cls: str, workload: str, requester: str | None = None) -> bool:
        """Does booking `b` keep a new load of `workload`, class `cls`, off the card?"""
        if cls == "peter":
            return False
        if b["class"] == "peter":
            return True
        if workload not in b["workloads"] and "*" not in b["workloads"]:
            return True
        return requester is not None and cls != "booked" and requester != b["owner"]

    def admit(self, workload: str, cls: str | None = None, requester: str | None = None,
              now: float | None = None) -> dict:
        """May a new load of `workload` start now? {ok, reason, until, booking}."""
        now = self.now() if now is None else now
        cls = cls or self.class_of(workload)
        for b, _s, e in self.active(now):
            if self.excludes(b, cls, workload, requester):
                whose = "the owner's time" if b["class"] == "peter" else f"{', '.join(b['workloads'])}"
                return {"ok": False, "until": e, "booking": b,
                        "reason": (f"the card is booked until {clock(e)} for {b['owner']} ({whose}"
                                   + (f": {b['note']}" if b["note"] else "") + f"; booking {b['id']}); "
                                   f"a new {workload} load of class {cls} waits")}
        return {"ok": True, "until": None, "booking": None, "reason": ""}

    def when(self, workload: str, cls: str | None = None, duration_s: float = 0,
             requester: str | None = None, now: float | None = None, horizon_s: float = 8 * DAY) -> dict:
        """The earliest time a run of `duration_s` could start and not run into a booking that
        excludes it. {now: bool, start, reason, bookings}."""
        now = self.now() if now is None else now
        cls = cls or self.class_of(workload)
        blocks = sorted(((s, e, b) for b in self.bookings() for s, e in self.windows(b, now, now + horizon_s)
                         if self.excludes(b, cls, workload, requester)), key=lambda x: x[0])
        t, why = now, []
        changed = True
        while changed:
            changed = False
            for s, e, b in blocks:
                if s < t + max(duration_s, 1) and e > t:
                    t = e
                    why.append(f"booking {b['id']} ({b['owner']}) {clock(s)}-{clock(e)}")
                    changed = True
            if t > now + horizon_s:
                return {"now": False, "start": None, "bookings": why,
                        "reason": f"no free window of that length in the next {int(horizon_s // DAY)} days"}
        if t <= now:
            return {"now": True, "start": now, "bookings": [], "reason": "nothing booked is in the way"}
        return {"now": False, "start": t, "bookings": why,
                "reason": "in the way: " + "; ".join(dict.fromkeys(why))}

    # --- tickets ----------------------------------------------------------------------------
    def tickets(self, include_done: bool = False) -> list[dict]:
        """Waiting and running tickets, oldest first; a ticket whose process has gone is expired
        on the way (and not returned)."""
        self.expire_dead()
        q = "SELECT * FROM ticket" + ("" if include_done else " WHERE state IN ('waiting','running')") \
            + " ORDER BY created, id"
        with self._db() as con:
            return [dict(r) for r in con.execute(q)]

    def expire_dead(self) -> int:
        now = self.now()
        with self._db() as con:
            rows = list(con.execute("SELECT id, pid FROM ticket WHERE state IN ('waiting','running')"))
            dead = [r["id"] for r in rows if not self.alive(r["pid"])]
            for tid in dead:
                con.execute("UPDATE ticket SET state = 'expired', ended = ? WHERE id = ?", (now, tid))
        return len(dead)

    def queue_view(self) -> list[dict]:
        """The waiting tickets in the order they'd be served (class, then age), and the running ones."""
        ts = self.tickets()
        running = [t for t in ts if t["state"] == "running"]
        waiting = sorted((t for t in ts if t["state"] == "waiting"), key=lambda t: (self.rank(t["class"]), t["created"]))
        return running + waiting
