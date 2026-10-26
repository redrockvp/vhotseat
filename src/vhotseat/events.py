"""The decision log: every switch, hold and gate decision, one JSON object per line."""

import datetime
import json
from pathlib import Path

from . import pressure, util

# The rows that say a claim is open, and what has been done about it.
CLAIM_ROWS = ("claim", "claim-end", "claim-release", "claim-release-targets",
              "claim-release-waiting")
# The rows that make an ask: `ask` opens the question (one per workload and requester at a
# time) and `ask-answer` closes it, answered by the owner or by nobody answering in time.
ASK_ROWS = ("ask", "ask-answer")


def row_time(e: dict) -> datetime.datetime | None:
    """A row's own time, or None if it can't be read: a row whose timestamp isn't usable isn't
    news about now, and isn't evidence of when something last happened either."""
    try:
        return datetime.datetime.fromisoformat(e["time"])
    except (KeyError, TypeError, ValueError):
        return None


class EventLog:
    def __init__(self, path: Path, keep: int = 500, telemetry=None):
        self.path = path
        self.keep = keep
        # Gets a copy of every event, kept for its retention window rather than `keep` lines.
        self.telemetry = telemetry

    def record(self, kind: str, **fields) -> None:
        """Append one event; the log is trimmed to the last `keep` lines."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {"time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "event": kind, **fields}
        lines = self._read() if self.path.exists() else []
        lines = [*lines[-(self.keep - 1):], json.dumps(event)]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        util.replace(tmp, self.path)
        if self.telemetry is not None:
            self.telemetry.event(event)

    def _read(self) -> list[str]:
        """The log's lines, retried the way `util.replace` retries the write beside it. On Windows
        the rename that appends a row leaves the path briefly unopenable by anyone else, and the
        asking makes that a everyday thing: a request waiting for an answer re-reads the log about
        twice a second, and so do the watchers, while whoever answers writes to it. Losing one of
        those reads to the write it crossed isn't information about the world, so it is tried
        again; the last attempt raises, because a log that can never be read is a real error. The
        wait is a fraction of the poll it happens in, never a whole one."""
        for attempt in range(20):
            try:
                return self.path.read_text(encoding="utf-8").splitlines()
            except PermissionError:
                if attempt == 19:
                    raise
                util.sleep(0.05)
        return []                                            # unreachable: the loop raises or returns

    def tail(self, limit: int = 200) -> list[dict]:
        """The last `limit` events, oldest first, skipping anything unreadable. A reader that
        wants the state of the world as recorded - which claims are still open, which ask is
        unanswered - reads this instead of keeping its own copy of it."""
        if not self.path.exists():
            return []
        out = []
        for line in self._read()[-limit:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    def claims_open(self, limit: int = 500) -> dict:
        """What the log shows as claimed right now: {(process, rule note): {claim, targets,
        kept, released, waiting, freed_mib}}, rebuilt from its tail.

        The log is the record, so it is what says whether a claim has been written, which
        workloads it was owed (`targets`), what has already been released for it, and what has
        already been reported waiting and how many times - rather than each process keeping its
        own copy of that. The trade is that a claim older than the retained log (`keep` lines)
        is forgotten, and recording it again is honest."""
        out: dict[tuple, dict] = {}

        def record(key):
            return out.setdefault(key, {"claim": {"name": key[0], "note": key[1]}, "targets": None,
                                        "kept": [], "released": [], "waiting": [], "freed_mib": 0})

        for e in self.tail(limit):
            kind = e.get("event")
            if kind not in CLAIM_ROWS:
                continue
            key = (e.get("name"), e.get("note"))
            if kind == "claim-end":
                out.pop(key, None)
            elif kind == "claim":
                record(key)["claim"] = e
            elif kind == "claim-release-targets":
                # Decided once, when the claim's release first ran. An empty list is a real
                # answer - nothing of ours was on the card then - so it isn't left as None.
                # `kept` names the workloads that were on the card and are not owed to this
                # claim because of their `yields_to:`: they keep their model, and the game is
                # short of it.
                record(key)["targets"] = list(e.get("targets") or [])
                record(key)["kept"] = list(e.get("kept") or [])
            elif kind == "claim-release":
                rec = record(key)
                got = set(e.get("workloads") or [])
                rec["released"] = sorted(set(rec["released"]) | got)
                rec["freed"] = (rec.get("freed") or []) + (e.get("freed") or [])
                rec["freed_mib"] += e.get("freed_mib") or 0
                # A retry that worked: the row saying it was still holding is history now.
                rec["waiting"] = [w for w in rec["waiting"] if w["workload"] not in got]
            elif e.get("workload"):
                rec = record(key)
                for w in rec["waiting"]:
                    if w["workload"] == e["workload"]:
                        # The newest reason, and however many tries the log has seen.
                        w["why"], w["stuck"] = e.get("why") or w["why"], bool(e.get("stuck")) or w["stuck"]
                        w["attempts"] = max(w.get("attempts") or 0, e.get("attempts") or 1)
                        w["held_mib"] = e.get("held_mib") or w.get("held_mib") or 0
                        break
                else:
                    rec["waiting"].append({"workload": e["workload"], "why": e.get("why") or "",
                                           "attempts": e.get("attempts") or 1,
                                           "stuck": bool(e.get("stuck")),
                                           "held_mib": e.get("held_mib") or 0})
        return out

    def asks(self, limit: int = 500) -> list[dict]:
        """Every ask the retained log holds, oldest first, each with its `answer` and `by` once it
        has one. Same shape of answer as `claims_open` and `episode_open`, for the same reason:
        `watch`, the dashboard, a gate in a fresh process and a toast's button all need the one
        open ask for a (workload, requester) pair, and whichever of them wrote it doesn't get a
        private copy of it. An ask older than the retained log is forgotten, and asking again is
        honest.

        A second `ask-answer` for the same ask - the owner clicking Keep a moment before a
        watcher gives up and applies the fallback - is ignored rather than argued about: the
        first answer in the log is the answer."""
        out: dict[str, dict] = {}
        order: list[str] = []
        for e in self.tail(limit):
            kind = e.get("event")
            if kind not in ASK_ROWS:
                continue
            ask_id = e.get("id")
            if not ask_id:
                continue
            if kind == "ask":
                if ask_id not in out:
                    out[ask_id] = dict(e)
                    order.append(ask_id)
            elif ask_id in out and not out[ask_id].get("answer"):
                out[ask_id].update(answer=e.get("answer"), by=e.get("by") or "owner",
                                   answered_at=e.get("time"))
        return [out[i] for i in order]

    def open_asks(self, limit: int = 500) -> list[dict]:
        """The asks the log shows as unanswered. What `/api/status` shows as the open question, and
        what an actor joins instead of asking the same question twice."""
        return [a for a in self.asks(limit) if not a.get("answer")]

    def open_ask(self, workload: str, requester: str, limit: int = 500) -> dict | None:
        """The unanswered ask for this workload and this requester, if the log holds one. The one
        question per pair is what stops a job queue asking the owner the same thing fifty times."""
        return next((a for a in self.open_asks(limit)
                     if a.get("workload") == workload and a.get("requester") == requester), None)

    def ask_by_id(self, ask_id: str, limit: int = 500) -> dict | None:
        """One ask by its id, answered or not: what `POST /api/asks/<id>` answers, and what says it
        was answered already (409) or is not in the log at all (404)."""
        return next((a for a in self.asks(limit) if a.get("id") == ask_id), None)

    def kept_said(self, reason: str, name: str | None = None, note: str | None = None,
                  since: str | None = None, limit: int = 500) -> set[str]:
        """Which workloads have already had their `policy-kept` row written for this reason - this
        claim (by name and note) or this episode (by grower name, at or after `since`).

        The row is what tells the owner a workload is still holding memory someone may be short
        of, so it has to be said once per reason, not once per poll: a watcher that re-checks an
        open claim every 2 s would otherwise bury the log in the same warning, and the dashboard
        banner decides what is news from a row's age."""
        out = set()
        for e in self.tail(limit):
            if e.get("event") != "policy-kept" or e.get("reason") != reason:
                continue
            if reason == "claims" and (e.get("name"), e.get("note")) != (name, note):
                continue
            if reason == "pressure" and e.get("name") != name:
                continue
            when, after = row_time(e), row_time({"time": since}) if since else None
            if after and (when is None or when < after):
                continue
            if e.get("workload"):
                out.add(e["workload"])
        return out

    def recent(self, kinds, within_s: float = 300.0, limit: int = 500) -> list[dict]:
        """Rows of these kinds no older than `within_s`, newest first: what the dashboard
        shows as a warning. The log is the record, so "is this still news" is answered from
        the row's own timestamp rather than from anything a process remembers - which is also
        what means both the dashboard and `watch` see the same warnings, and whichever of
        them wrote the row doesn't get a private copy of it."""
        cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(seconds=within_s)
        out = []
        for e in reversed(self.tail(limit)):
            if e.get("event") not in kinds:
                continue
            when = row_time(e)
            if when is not None and when >= cutoff:
                out.append(e)
        return out

    def episode_open(self, limit: int = 500) -> dict | None:
        """The pressure episode the log shows as open, or None: {episode, targets, released,
        waiting, freed_mib}, rebuilt from its rows.

        Same reason as `claims_open`, and the same shape of answer: the episode is one process's
        growth decided once, the workloads it was owed were chosen then, and what has been released
        and what is still holding come from the rows rather than from the memory of whichever
        process wrote them. So `watch` and the dashboard - which both run this check and both act -
        see one episode, and the one that arrives second finds it already in progress instead of
        starting a second one and unloading something again.

        The trade is the same one: an episode older than the retained log (`keep` lines) is
        forgotten, and the next poll treats the card being short as news.
        """
        out: dict | None = None
        for e in self.tail(limit):
            kind = e.get("event")
            if kind not in pressure.EPISODE_ROWS:
                continue
            if kind == "pressure":
                out = {"episode": e, "targets": list(e.get("targets") or []), "released": [],
                       "waiting": [], "freed_mib": 0}
            elif kind == "pressure-end" and out:
                out = None
            elif kind == "pressure-release" and out:
                got = set(e.get("workloads") or [])
                out["released"] = sorted(set(out["released"]) | got)
                out["freed_mib"] += e.get("freed_mib") or 0
                out["waiting"] = [w for w in out["waiting"] if w["workload"] not in got]
            elif kind == "pressure-release-waiting" and out:
                for w in out["waiting"]:
                    if w["workload"] == e.get("workload"):
                        w["why"], w["stuck"] = e.get("why") or w["why"], bool(e.get("stuck")) or w["stuck"]
                        w["attempts"] = max(w.get("attempts") or 0, e.get("attempts") or 1)
                        w["held_mib"] = e.get("held_mib") or w.get("held_mib") or 0
                        break
                else:
                    out["waiting"].append({"workload": e.get("workload") or "", "why": e.get("why") or "",
                                           "attempts": e.get("attempts") or 1,
                                           "stuck": bool(e.get("stuck")), "held_mib": e.get("held_mib") or 0})
        return out

    def last_episode(self, limit: int = 500) -> dict | None:
        """The newest pressure row of any kind, open or closed. `cooldown_s` is measured from it:
        an episode that ended a minute ago is not a clean slate, because the memory that was
        released has to be reloaded before the card is in the state the next release would fix."""
        for e in reversed(self.tail(limit)):
            if e.get("event") in pressure.EPISODE_ROWS and row_time(e) is not None:
                return e
        return None

    def last(self) -> dict | None:
        if not self.path.exists():
            return None
        lines = self.path.read_text(encoding="utf-8").splitlines()
        try:
            return json.loads(lines[-1]) if lines else None
        except json.JSONDecodeError:
            return None
