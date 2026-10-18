"""vhotseat as a library, for gates that run in a long-lived process (a proxy in
front of a server with no pre-load hook: docs/guides/proxy-gate.md). Spawning
`vhotseat switch` per request would cost ~100 ms and a process each time; this
costs one small file read on the fast path.

    from vhotseat import api

    seat = api.open()                    # the config at ~/.config/vhotseat/config.yaml
    if not seat.owns("llama"):           # cheap while nothing changed hands (see below)
        seat.acquire("llama", wait=120)  # the slow path: make room
    ... forward the request ...

`owns()` trusts its last answer while the handover count (the generation) is
unchanged: a small file read, no lock, no HTTP. Every switch, hold and gate that
makes room bumps it, so a cached "llama owns the GPU" is dropped the moment
anything else takes it. On a cache miss it probes every workload, including the
one the caller is a proxy for, so it calls that proxy's own status endpoint:
don't call it while holding a lock that endpoint needs.

All waits and refusals follow the same rules as the CLI: running work is never
interrupted, and acquire() raises Busy (or SwitchError) instead.
"""

import os
from pathlib import Path

from . import config as cfgmod
from .arbiter import NONE, Arbiter
from .errors import Busy, ReleaseFailed, SwitchError
from .setup import build

__all__ = ["Seat", "open", "Busy", "SwitchError", "ReleaseFailed"]


class Seat:
    """One GPU, as a long-running gate sees it."""

    def __init__(self, arbiter: Arbiter):
        self.arbiter = arbiter
        self._cache: tuple[int, str] | None = None   # (generation, owner)

    def generation(self) -> int:
        return self.arbiter.lock.generation()

    def holder(self) -> str:
        """What a switch or hold holding the lock is doing right now, "" if none."""
        return self.arbiter.lock.holder()

    def owner(self) -> str:
        """Who holds the GPU now: a workload's name, "none" or "conflict" (a full probe)."""
        return self.arbiter.probe()["mode"]

    def owns(self, name: str) -> bool:
        """Whether `name` holds the GPU, answered from cache while nothing has
        changed hands since it was last checked. A switch or hold in progress
        (for anyone else) always means no."""
        holder = self.holder()
        if holder and holder not in (f"switch {name}", f"hold {name}"):
            return False
        gen = self.generation()
        if self._cache and self._cache[0] == gen:
            return self._cache[1] == name
        owner = self.owner()
        self._cache = (gen, owner)
        return owner == name

    def acquire(self, name: str, wait: float = 0) -> list[str]:
        """Give the GPU to `name` under the lock, as `vhotseat switch NAME --wait
        WAIT` does, without loading it (the request about to be forwarded does).
        Returns what was done; raises Busy or SwitchError if it can't."""
        if name not in self.arbiter.workloads:
            raise KeyError(f"no workload named {name!r}")
        return self._switch(name, wait)

    def release_all(self, wait: float = 0) -> list[str]:
        """Free the GPU (`vhotseat switch none`)."""
        return self._switch(NONE, wait)

    def _switch(self, name: str, wait: float) -> list[str]:
        result = self.arbiter.switch(name, wait, load=False)
        self._cache = None
        if not result["ok"]:
            raise result.get("exception") or SwitchError(result["error"])
        return result.get("done", [])


def open(config_path: str | os.PathLike | None = None) -> Seat:  # noqa: A001 (mirrors builtins on purpose)
    path = Path(config_path) if config_path else cfgmod.default_path()
    return Seat(build(cfgmod.load(path)))
