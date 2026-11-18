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
STALE_S = 120.0

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


def cfg(raw: dict | None, owners=()) -> dict | None:
    """The `schedule:` block with its defaults, or None when the config has none.

    `owners` are the workloads whose untagged requests are the owner's own (class `peter`) unless
    `default_class` says otherwise: the ComfyUI workloads, whose untagged jobs come from its web page
    (the owner's decision 4). An agent's batch takes a ticket instead (`comfy-render --hold`)."""
    if raw is None:
        return None
    classes = [dict(c) for c in (raw.get("classes") or DEFAULT_CLASSES)]
    default_class = {w: "peter" for w in owners}
    default_class.update(raw.get("default_class") or {})
    return {"classes": classes, "default_class": default_class,
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
        # Every gate reads this, inside a workload's own load limit and sometimes under the GPU
        # lock: a writer that won't let go costs at most 2 s, then the read fails open (Arbiter).
        con = sqlite3.connect(self.path, timeout=2)
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
                # Two series repeat within a week; a one-off of the owner's may be further out.
                to = end if not every else max(start + 14 * DAY, b["end"] if not b["every"] else 0)
                for s, e in self.windows(b, start, to):
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
            if k > 3660:
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
        if until <= now:
            raise ScheduleError(f"{clock(until)} has already passed: pausing until then changes nothing")
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
    def take(self, requester: str, cls: str, workload: str, *, est_s: float | None = None,
             pid: int | None = None, model: str | None = None, note: str = "") -> dict:
        """Queue one request as a ticket: waiting, stamped with the clock."""
        if cls not in self.classes:
            raise ScheduleError(f"a ticket's class is one of {', '.join(self.classes)}, not {cls!r}")
        if not workload:
            raise ScheduleError("a ticket has to name the workload it runs on")
        now = self.now()
        with self._db() as con:
            cur = con.execute("INSERT INTO ticket (requester, class, workload, model, est_s, pid, "
                              "state, created, heartbeat, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (requester, cls, workload, model, est_s, pid, "waiting", now, now, note))
            tid = cur.lastrowid
        return self.ticket(tid)

    def ticket(self, tid: int) -> dict | None:
        """One ticket as a dict, in whatever state it is; None when there is no such id."""
        with self._db() as con:
            r = con.execute("SELECT * FROM ticket WHERE id = ?", (tid,)).fetchone()
        return dict(r) if r else None

    def beat(self, tid: int) -> dict:
        """Say a ticket without a pid is still there (its caller lives elsewhere): heartbeat = now."""
        with self._db() as con:
            con.execute("UPDATE ticket SET heartbeat = ? WHERE id = ?", (self.now(), tid))
        return self.ticket(tid)

    def start(self, tid: int) -> dict:
        """A waiting ticket goes running now."""
        with self._db() as con:
            r = con.execute("SELECT * FROM ticket WHERE id = ?", (tid,)).fetchone()
            if not r:
                raise ScheduleError(f"no ticket {tid}")
            if r["state"] != "waiting":
                raise ScheduleError(f"ticket {tid} is {r['state']}, not waiting: only a waiting ticket starts")
            con.execute("UPDATE ticket SET state = 'running', started = ? WHERE id = ?", (self.now(), tid))
        return self.ticket(tid)

    def finish(self, tid: int, state: str = "done") -> dict:
        """Close a ticket: done, yielded (it gave the card up), or expired."""
        if state not in ("done", "yielded", "expired"):
            raise ScheduleError(f"a ticket finishes as done, yielded, or expired, not {state!r}")
        with self._db() as con:
            r = con.execute("SELECT * FROM ticket WHERE id = ?", (tid,)).fetchone()
            if not r:
                raise ScheduleError(f"no ticket {tid}")
            con.execute("UPDATE ticket SET state = ?, ended = ? WHERE id = ?", (state, self.now(), tid))
        return self.ticket(tid)

    def tickets(self, include_done: bool = False) -> list[dict]:
        """Waiting and running tickets, oldest first; a ticket whose process has gone is expired
        on the way (and not returned)."""
        self.expire_dead()
        q = "SELECT * FROM ticket" + ("" if include_done else " WHERE state IN ('waiting','running')") \
            + " ORDER BY created, id"
        with self._db() as con:
            return [dict(r) for r in con.execute(q)]

    def expire_dead(self) -> int:
        """Expire tickets that stopped living: a pid that has gone, or a heartbeat past `STALE_S`."""
        now = self.now()
        with self._db() as con:
            rows = list(con.execute("SELECT id, pid, heartbeat FROM ticket WHERE state IN ('waiting','running')"))
            dead = [r["id"] for r in rows if not self._living(r, now)]
            for tid in dead:
                con.execute("UPDATE ticket SET state = 'expired', ended = ? WHERE id = ?", (now, tid))
        return len(dead)

    def _living(self, r, now: float) -> bool:
        """A ticket with a pid lives while its process does; one without lives on its heartbeat."""
        if r["pid"] is not None:
            return bool(self.alive(r["pid"]))
        return now - r["heartbeat"] < STALE_S

    def order(self, on_card: str | None = None, now: float | None = None) -> list[dict]:
        """The waiting tickets in the order they would be served.

        Class first, then affinity (a ticket for the workload already on the card goes first, and one
        that has waited past `affinity_s` stops paying for a handover), then round-robin: the ticket's
        place among its own requester's waiting tickets of that class, so one busy caller cannot keep
        another waiting behind all of its tickets. Age breaks the rest.
        """
        now = self.now() if now is None else now
        waiting = [t for t in self.tickets() if t["state"] == "waiting"]
        seen: dict[tuple[str, str], int] = {}
        k = {}
        for t in sorted(waiting, key=lambda t: (t["created"], t["id"])):
            g = (t["requester"], t["class"])
            k[t["id"]] = seen.get(g, 0)
            seen[g] = k[t["id"]] + 1
        affinity = self.conf["affinity_s"]
        return sorted(waiting, key=lambda t: (
            self.rank(t["class"]),
            0 if on_card is None or t["workload"] == on_card or now - t["created"] >= affinity else 1,
            k[t["id"]], t["created"], t["id"]))

    def head(self, on_card: str | None = None) -> dict | None:
        """The next ticket that can actually start now, or None when none of them can.

        A ticket without an estimate is judged on its class's `max_hold_s`, so a run nobody sized
        waits for a booking the way its class would be cut off anyway.
        """
        for t in self.order(on_card):
            est = t["est_s"]
            if est is None:
                est = self.max_hold_s(t["class"])
                if est is None:
                    est = 0
            if (self.admit(t["workload"], t["class"], t["requester"])["ok"]
                    and self.when(t["workload"], t["class"], est, t["requester"])["now"]):
                return t
        return None

    def my_turn(self, tid: int, on_card: str | None = None) -> bool:
        """Is this ticket the one that takes the card now? Nobody else may be running."""
        h = self.head(on_card)
        if h is None or h["id"] != tid:
            return False
        return not any(t["state"] == "running" and t["id"] != tid for t in self.tickets())

    def claim(self, tid: int, on_card: str | None = None) -> bool:
        """Start this ticket when it is its turn: the update itself checks, so two claimers
        that race over the same queue still leave only one ticket running."""
        if not self.my_turn(tid, on_card):
            return False
        with self._db() as con:
            cur = con.execute("UPDATE ticket SET state = 'running', started = ? WHERE id = ? AND state = 'waiting'"
                              " AND NOT EXISTS (SELECT 1 FROM ticket WHERE state = 'running' AND id != ?)",
                              (self.now(), tid, tid))
            return cur.rowcount == 1

    def overdue(self, tid: int) -> dict | None:
        """A running ticket held past its class's max hold, with a class at its own rank or higher
        waiting: {held_s, limit_s, waiting}. None when it is not overdue.
        """
        t = self.ticket(tid)
        if t is None or t["state"] != "running":
            return None
        limit = self.max_hold_s(t["class"])
        if limit is None:
            return None
        held = self.now() - t["started"]
        if held <= limit:
            return None
        waiting = sum(1 for w in self.tickets()
                      if w["state"] == "waiting" and self.rank(w["class"]) <= self.rank(t["class"]))
        return {"held_s": held, "limit_s": limit, "waiting": waiting} if waiting else None

    def should_yield(self, tid: int) -> bool:
        """Has this running ticket held the card long enough that a waiting peer wants it back?"""
        return self.overdue(tid) is not None

    def queue_view(self, on_card: str | None = None) -> list[dict]:
        """The running tickets, then the waiting ones in the order they'd be served."""
        ts = self.tickets()
        running = [t for t in ts if t["state"] == "running"]
        return running + self.order(on_card)
