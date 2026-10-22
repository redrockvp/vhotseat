"""The decision log: every switch, hold and gate decision, one JSON object per line."""

import datetime
import json
from pathlib import Path

from . import util

# The rows that say a claim is open, and what has been done about it.
CLAIM_ROWS = ("claim", "claim-end", "claim-release", "claim-release-targets",
              "claim-release-waiting")


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
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        lines = [*lines[-(self.keep - 1):], json.dumps(event)]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        util.replace(tmp, self.path)
        if self.telemetry is not None:
            self.telemetry.event(event)

    def tail(self, limit: int = 200) -> list[dict]:
        """The last `limit` events, oldest first, skipping anything unreadable. A reader that
        wants the state of the world as recorded - which claims are still open - reads this
        instead of keeping its own copy of it."""
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines()[-limit:]:
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
            try:
                when = datetime.datetime.fromisoformat(e["time"])
                fresh = when >= cutoff
            except (KeyError, TypeError, ValueError):
                continue            # a row whose time can't be read isn't news about now
            if fresh:
                out.append(e)
        return out

    def last(self) -> dict | None:
        if not self.path.exists():
            return None
        lines = self.path.read_text(encoding="utf-8").splitlines()
        try:
            return json.loads(lines[-1]) if lines else None
        except json.JSONDecodeError:
            return None
