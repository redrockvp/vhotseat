"""The decision log: every switch, hold and gate decision, one JSON object per line."""

import datetime
import json
from pathlib import Path


class EventLog:
    def __init__(self, path: Path, keep: int = 500):
        self.path = path
        self.keep = keep

    def record(self, kind: str, **fields) -> None:
        """Append one event; the log is trimmed to the last `keep` lines."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {"time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "event": kind, **fields}
        lines = self.path.read_text().splitlines() if self.path.exists() else []
        lines = [*lines[-(self.keep - 1):], json.dumps(event)]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(self.path)

    def last(self) -> dict | None:
        if not self.path.exists():
            return None
        lines = self.path.read_text().splitlines()
        try:
            return json.loads(lines[-1]) if lines else None
        except json.JSONDecodeError:
            return None
