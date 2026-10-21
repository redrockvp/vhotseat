"""The decision log: every switch, hold and gate decision, one JSON object per line."""

import datetime
import json
from pathlib import Path

from . import util

# The rows that say a claim is open, and what has been done about it.
CLAIM_ROWS = ("claim", "claim-end", "claim-release", "claim-release-waiting")


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
        """What the log shows as claimed right now: {(process, rule note): {claim, released,
        waiting, freed_mib}}, rebuilt from its tail.

        The log is the record, so it is what says whether a claim has been written, what has
        already been released for it, and what has already been reported waiting - rather than
        each process keeping its own copy of that. The trade is that a claim older than the
        retained log (`keep` lines) is forgotten, and recording it again is honest."""
        out: dict[tuple, dict] = {}

        def record(key):
            return out.setdefault(key, {"claim": {"name": key[0], "note": key[1]},
                                        "released": [], "waiting": [], "freed_mib": 0})

        for e in self.tail(limit):
            kind = e.get("event")
            if kind not in CLAIM_ROWS:
                continue
            key = (e.get("name"), e.get("note"))
            if kind == "claim-end":
                out.pop(key, None)
            elif kind == "claim":
                record(key)["claim"] = e
            elif kind == "claim-release":
                rec = record(key)
                rec["released"] = sorted(set(rec["released"]) | set(e.get("workloads") or []))
                rec["freed_mib"] += e.get("freed_mib") or 0
            elif e.get("workload"):
                rec = record(key)
                if e["workload"] not in [w["workload"] for w in rec["waiting"]]:
                    rec["waiting"].append({"workload": e["workload"], "why": e.get("why") or ""})
        return out

    def last(self) -> dict | None:
        if not self.path.exists():
            return None
        lines = self.path.read_text(encoding="utf-8").splitlines()
        try:
            return json.loads(lines[-1]) if lines else None
        except json.JSONDecodeError:
            return None
