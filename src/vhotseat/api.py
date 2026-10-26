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
anything else takes it. What `acquire()` remembered - "I just gave this to that
gate, its weights are still landing" - is good for one load window, and is
withdrawn the moment something claims the card (see `LOAD_WINDOW_S`). On a cache
miss it probes every workload, including the one the caller is a proxy for, so it
calls that proxy's own status endpoint: don't call it while holding a lock that
endpoint needs.

All waits and refusals follow the same rules as the CLI: running work is never
interrupted, and acquire() raises Busy (or SwitchError) instead.
"""

import os
import time
from pathlib import Path

from . import config as cfgmod
from .arbiter import NONE, Arbiter
from .errors import Busy, ReleaseFailed, SwitchError
from .setup import build

__all__ = ["Busy", "ReleaseFailed", "Seat", "SwitchError", "open"]

# How long a gate's own claim outlives the handover that made room for it, before the seat looks at
# the card again: the window in which the request it forwarded is expected to put its weights on it.
# Five minutes is a slow box's model load plus a queue that may have sat behind work in flight, and
# far shorter than the wait for a load that was never coming. It has to run out: when nothing
# loaded, nothing bumped the generation, and a claim that starts afterwards with nothing of ours to
# release takes no lock and bumps nothing either. A claim that never expires has the seat vouching
# for a card nobody holds, and every request that believes it skips the claim check and loads on top
# of a game.
LOAD_WINDOW_S = 300.0

# The clock that window is measured on, at module level so a test can hand the seat a longer minute.
_now = time.monotonic


class Seat:
    """One GPU, as a long-running gate sees it."""

    def __init__(self, arbiter: Arbiter):
        self.arbiter = arbiter
        self._cache: tuple[int, str] | None = None   # (generation, owner)
        # (generation, name, when the seat stops vouching for it): what we took, for one window.
        self._acquired: tuple[int, str, float] | None = None

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
        if (self._acquired and self._acquired[:2] == (gen, name)
                and _now() < self._acquired[2] and not self.arbiter.claim_hits()):
            # We took the card and nothing has changed hands since. For a server that loads
            # on request this is the window where its weights are still landing: a probe
            # would say nobody holds the GPU, and a burst of gated requests would each take
            # the handover again. The seat remembers who it just gave it to - for
            # LOAD_WINDOW_S, and not while something owns the card by right. A claim is
            # checked here rather than left to the handover this fast path exists to skip:
            # a claim that had nothing of ours to release leaves the generation where it was.
            # The scan is the price of that, paid only inside the window.
            return True
        if self._cache and self._cache[0] == gen:
            return self._cache[1] == name
        owner = self.owner()
        self._cache = (gen, owner)
        return owner == name

    def acquire(self, name: str, wait: float = 0) -> list[str]:
        """Give the GPU to `name` under the lock, as `vhotseat switch NAME --wait
        WAIT` does, without loading it (the request about to be forwarded does).
        Returns what was done; raises Busy or SwitchError if it can't - and waits for the
        owner's answer first if a holder's policy says Ask me for this workload (see
        `arbiter.ask_before_request`: the answer arrives in the event log, so this needs no
        channel back to the proxy).

        A proxy is also the only thing on some boxes that knows a request arrived, so it stamps
        the workload as used here: that is the fact an ask is asked with ("used 40 s ago, so an
        agent may be mid-task"), and a server whose traffic only goes through a gate has no
        watcher that would otherwise have seen it."""
        if name not in self.arbiter.workloads:
            raise KeyError(f"no workload named {name!r}")
        self.arbiter.note_used(name)
        return self._switch(name, wait)

    def release_all(self, wait: float = 0) -> list[str]:
        """Free the GPU (`vhotseat switch none`)."""
        return self._switch(NONE, wait)

    def _switch(self, name: str, wait: float) -> list[str]:
        # A Seat is opened once and used by every request that follows, so it is the one reader of
        # the policy on this box that isn't a fresh process: an owner who saves the policy from the
        # dashboard shouldn't have to restart their proxy for the gate to obey it. Until the file
        # changes this is one `stat`, and a file that no longer reads keeps the rules that were.
        self.arbiter.reload_policy()
        # A gate asking through the Seat API is a load nobody explicitly chose, so it is a
        # request and claims refuse it: a proxy must not put weights on the card under a game.
        # A typed `vhotseat switch` is an order, and goes through switch() without this flag.
        # Letting go of the card is not a load: a claim must never stop work getting *off* the
        # card, which is also the door the release-on-claim-start (phase 1) goes through.
        result = self.arbiter.switch(name, wait, load=False, request=(name != NONE))
        self._cache = None
        self._acquired = None
        if not result["ok"]:
            raise result.get("exception") or SwitchError(result["error"])
        self._acquired = (self.generation(), name, _now() + LOAD_WINDOW_S)
        return result.get("done", [])


def open(config_path: str | os.PathLike | None = None) -> Seat:  # mirrors builtins.open on purpose
    path = Path(config_path) if config_path else cfgmod.default_path()
    # The path goes to the arbiter as well as the config: a Seat is held across many requests, so
    # it is the one reader on this list that isn't a fresh process, and it needs to know which file
    # its rules came from. `arbiter.reload_policy()` is what picks up a save; the gate's own
    # decisions here are as of the last time it was called.
    return Seat(build(cfgmod.load(path), config_path=path))
