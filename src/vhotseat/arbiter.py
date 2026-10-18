"""The arbiter: who owns the GPU, and handing it from one workload to another.

Every entry point goes through here:
  acquire  make room for a workload: release the others, restart one if that
           isn't enough for its admission, check admission (used by switch, hold)
  gate     a workload's own pre-load hook asks whether it may load
  switch   acquire under the lock, from the CLI or the ComfyUI guard; or, for
           "none", release every workload
  hold     acquire, then keep the lock until a command exits

Running work is never interrupted: a busy workload is waited for (up to `wait`)
or the request is refused. Ownership is derived from the live workloads each
time, never stored, so it can't drift.
"""

import os
import signal
import subprocess
import time

from . import host, util
from .drivers.base import Workload
from .errors import ReleaseFailed, SwitchError
from .events import EventLog
from .lock import GpuLock, Held

NONE = "none"
# Set in a hold's command's environment, so a nested switch to the held workload
# is a no-op instead of waiting on the lock the hold owns.
HOLD_ENV = "VHOTSEAT_HOLD"


class Arbiter:
    def __init__(self, workloads: list[Workload], gpu, lock: GpuLock, events: EventLog, *,
                 gate_release_timeout_s: float = 20, release_timeout_s: float = 60,
                 machine_ram=host.machine_ram):
        self.workloads = {w.name: w for w in workloads}
        self.gpu = gpu
        self.lock = lock
        self.events = events
        # A gate runs inside the workload's own load, which has a time limit
        # (Strata's before_load_timeout_s), so its releases get less time.
        self.gate_release_timeout_s = gate_release_timeout_s
        self.release_timeout_s = release_timeout_s
        self.machine_ram = machine_ram

    def snapshot(self) -> dict:
        return self.gpu.snapshot({n: w.vram_selector for n, w in self.workloads.items()})

    def _others(self, name: str | None) -> list[Workload]:
        return [w for n, w in self.workloads.items() if n != name]

    # --- status --------------------------------------------------------------

    def probe(self) -> dict:
        gpu = self.snapshot()
        states = {n: w.probe(gpu) for n, w in self.workloads.items()}
        active = [n for n, w in self.workloads.items() if w.active(states[n], gpu)]
        mode = "conflict" if len(active) > 1 else active[0] if active else NONE
        ram = self.machine_ram()
        ram.update({n: w.memory() for n, w in self.workloads.items()})
        return {"mode": mode, **states, "gpu": gpu, "ram": ram, "last_event": self.events.last()}

    def describe(self, s: dict) -> str:
        g = s["gpu"]
        width = max(len(n) for n in [*self.workloads, "mode"]) + 2
        lines = [f"{'mode:':<{width}}{s['mode']}"]
        lines += [f"{n + ':':<{width}}{w.describe(s[n])}" for n, w in self.workloads.items()]
        shares = ", ".join(f"{n} {g[f'{n}_mib']}" for n in self.workloads)
        lines.append(f"{'gpu:':<{width}}" + (g["error"] if "error" in g else
                     f"{g['free_mib']} of {g['total_mib']} MiB free ({shares}, other {g['other_mib']})"))
        lines.append(f"{'ram:':<{width}}" + self._describe_ram(s["ram"]))
        if ev := s.get("last_event"):
            detail = ", ".join(f"{k}={v}" for k, v in ev.items() if k not in ("time", "event"))
            lines.append(f"{'last:':<{width}}{ev['time']} {ev['event']}" + (f" ({detail})" if detail else ""))
        return "\n".join(lines)

    def _describe_ram(self, r: dict) -> str:
        if "error" in r:
            return r["error"]

        def gib(mib):
            return f"{mib / 1024:.1f}"

        def share(name):
            m = r[name]
            if m["ram_mib"] is None:
                return f"{name} -"
            swap = f" + {gib(m['swap_mib'])} swap" if m["swap_mib"] else ""
            return f"{name} {gib(m['ram_mib'])} GiB{swap}"

        return (f"{gib(r['available_mib'])} of {gib(r['total_mib'])} GiB available, "
                f"swap {gib(r['swap_used_mib'])} of {gib(r['swap_total_mib'])} GiB used "
                f"({', '.join(share(n) for n in self.workloads)})")

    # --- handing over ----------------------------------------------------------

    def _shortfall(self, x: Workload | None) -> str | None:
        """What `x`'s admission is missing right now, or None if it's met."""
        if x is None or not x.admission:
            return None
        adm, model = x.admission_for()
        who = f"{x.label} ({model})" if model else x.label
        if (need := adm.get("min_available_ram_mib")) is not None:
            have = self.machine_ram().get("available_mib")
            if have is None or have < need:
                return f"{who} needs {need} MiB of available RAM, {have} MiB is available"
        if (need := adm.get("min_free_vram_mib")) is not None:
            have = self.snapshot().get("free_mib")
            if have is None or have < need:
                return f"{who} needs {need} MiB of free VRAM, {have} MiB is free"
        return None

    def _make_room(self, x: Workload | None, wait: float, timeout: float, ensure: bool,
                   deadline: float | None = None, admit: bool = True) -> list[str]:
        """Release every workload but `x`, restarting one whose release isn't
        enough when it allows that; then, if `admit`, restart idle ones while
        `x`'s admission isn't met. Returns what was done, for the event log.

        Admission is for a workload about to load: one already holding the GPU
        would count its own memory against itself."""
        others = self._others(x.name if x else None)
        done = []
        for y in others:
            try:
                if y.service_active():
                    if ensure:
                        y.ensure_running()
                    try:
                        done += y.release(wait, timeout)
                    except ReleaseFailed as e:
                        if not y.allow_restart:
                            raise
                        done += e.done
                        y.restart(e.why)
                        done.append(e.then)
                elif y.vram_mib() > y.idle_vram_mib:
                    raise SwitchError(f"a {y.label} process holds VRAM but {y.service.unit or 'its service'} "
                                      "is stopped; kill it by hand")
            except SwitchError as e:
                e.workload = y
                raise
        # RAM isn't exclusive: a released workload's process may still hold what x needs.
        tried = set()
        while admit and (short := self._shortfall(x)):
            candidates = [y for y in others if y.allow_restart and y.name not in tried
                          and y.service_active() and not y.busy()]
            if not candidates:
                raise SwitchError(short)
            # A restart takes ~15-20 s (ComfyUI); don't start one the gate can't finish.
            if deadline is not None and time.monotonic() + 20 * util.SCALE > deadline:
                raise SwitchError(f"{short}; no time left to restart anything")
            y = max(candidates, key=lambda c: c.held_mib())
            tried.add(y.name)
            held = y.held_mib()
            try:
                y.restart(f"{short}; it holds {held} MiB of RAM + swap")
            except SwitchError as e:
                e.workload = y
                raise
            done.append(f"restarted {y.label} to release {held} MiB of RAM + swap for {x.label}")
        return done

    def acquire(self, name: str, wait: float, load: bool) -> list[str]:
        """Give the GPU to `name` (or to nobody, for "none"): release the others,
        start it if it's stopped, check its admission, and load it if `load`."""
        x = self.workloads.get(name)
        others = self._others(name)
        s = self.probe()
        resident = x is not None and x.active(s[name], s["gpu"])
        # A workload that may be loading invisibly is asked to release even when the
        # GPU looks like x's already, so this only short-cuts when none of them may be.
        if (s["mode"] == name or (s["mode"] == NONE and x is not None and not x.can_load())) \
                and not any(y.release_always for y in others):
            util.log(f"already in {name} mode")
            return []
        done = self._make_room(x, wait, self.release_timeout_s, ensure=True, admit=not resident)
        self.lock.bump()
        if x is None:
            return done
        x.ensure_running()
        loads = load and x.can_load()
        if loads:
            x.load(wait)
        # A lazy workload loads on its first job, so after a switch the GPU may be
        # free ("none") rather than its: the switch is done once the others are gone.
        mode = self.probe()["mode"]
        if mode != name and (loads or mode != NONE):
            what = (f"{x.label} reported loaded" if loads
                    else f"{', '.join(y.label for y in others)} unloaded")
            raise SwitchError(f"{what}, but the mode is {mode}, not {name}; run `vhotseat status`")
        return done

    def gate(self, name: str) -> bool:
        """Run inside `name`'s own load (Strata's before_load): may it load?

        - A `switch name` or `hold name` holds the lock: this is the load it
          asked for, or it already gave the GPU to `name`. Allow at once.
          Waiting would deadlock: that switch is blocked on this very load.
        - Anything else holds the lock: refuse.
        - Otherwise a request woke the workload. Another workload busy, or its
          state unknown: refuse, a render is never interrupted. All idle:
          release them (restarting one if releasing isn't enough), check
          admission, then allow.

        It makes room itself rather than calling `switch`: that ends in loading
        `name`, which would wait on the very load running this gate. The lock is
        held throughout, so a switch starting now waits for it.
        """
        x = self.workloads[name]
        others = self._others(name)
        kind = f"{name}-load"
        got = self.lock.try_take(f"gate {name}")
        if not isinstance(got, Held):
            holder = got or "a switch"
            allowed = got in (f"switch {name}", f"hold {name}")
            self.events.record(kind, source="hook", allowed=allowed, reason=f"{holder} in progress")
            if not allowed:
                util.log(f"refusing {x.label} load: {holder} is in progress")
            return allowed

        # The hook's whole run must fit in the workload's own limit (gate.timeout).
        deadline = time.monotonic() + x.gate["timeout"] * util.SCALE if x.gate.get("timeout") else None
        with got:
            gpu = self.snapshot()
            reason = gpu.get("error") or next((r for y in others if (r := y.busy())), None)
            if not reason:
                try:
                    done = self._make_room(x, 0, self.gate_release_timeout_s, ensure=False, deadline=deadline)
                    self.lock.bump()
                except SwitchError as e:
                    reason = (f"could not make room in {e.workload.label}: {e}" if e.workload is not None
                              else str(e))
                else:
                    # What memory it loads into: oomd killed loads that skipped the restart.
                    self.events.record(kind, source="request", allowed=True,
                                       reason="; ".join([*(f"{y.label} idle" for y in others), *done]),
                                       available_mib=self.machine_ram()["available_mib"],
                                       **{y.name: y.memory() for y in others})
                    return True
            self.events.record(kind, source="request", allowed=False, reason=reason)
            util.log(f"refusing {x.label} load: {reason}")
            return False

    def _take(self, name: str, *, skip_if_held: bool):
        def waiting(_holder):
            util.log("waiting for another switch or hold to finish")
        unless = (lambda h: h == f"hold {name}") if skip_if_held else None
        return self.lock.take(f"switch {name}", unless=unless, on_wait=waiting)

    def switch(self, name: str, wait: float, load: bool = True) -> dict:
        """Move the GPU to `name` (or free it, for "none") under the lock. Returns
        {"ok": …} plus "held" or "error"; the caller prints status.

        While a `hold name` owns the lock, the GPU is already `name`'s and stays
        so: return at once. That's how a ComfyUI job queued by a held script gets
        past its guard; before, the guard waited on the hold, which was waiting
        on the job."""
        held = os.environ.get(HOLD_ENV)
        if held:
            # Inside `hold`: our parent owns the lock, so taking it would deadlock.
            if held != name:
                util.log(f"can't switch to {name} inside `hold {held}`")
                return {"ok": False, "error": f"inside hold {held}", "inside_hold": held}
            return {"ok": True, "held": held}
        lock = self._take(name, skip_if_held=True)
        if lock is None:
            return {"ok": True, "held": name}
        with lock:
            before = self.probe()["mode"]
            try:
                done = self.acquire(name, wait, load=load)
            except SwitchError as e:
                self.events.record("switch", to=name, ok=False, error=str(e))
                util.log(f"switch to {name} failed: {e}")
                return {"ok": False, "error": str(e), "exception": e}
        # Log a switch when the GPU changed hands. A lazy workload's switch leaves it
        # "none" (it loads on its first job), so "none" → it with nothing done is no change.
        x = self.workloads.get(name)
        lazy_and_free = before == NONE and x is not None and not x.can_load()
        if done or (before != name and not lazy_and_free):
            self.events.record("switch", to=name, ok=True, previous=before, **({"done": done} if done else {}))
        return {"ok": True, **({"done": done} if done else {})}

    def hold(self, name: str, command: list[str], wait: float) -> int:
        """Switch to `name` and keep the GPU there until `command` exits; returns
        its exit code. The lock is marked "hold name" once the switch is done, so
        every other workload's gate refuses, and a script may leave the queue
        empty between its jobs."""
        if os.environ.get(HOLD_ENV) == name:
            # Already inside a hold: our parent owns the lock, so taking it would deadlock.
            return subprocess.call(command)
        with self._take(name, skip_if_held=False) as lock:
            before = self.probe()["mode"]
            try:
                self.acquire(name, wait, load=False)
            except SwitchError as e:
                self.events.record("hold", mode=name, ok=False, error=str(e))
                util.log(f"hold {name} failed: {e}")
                return 1
            # Marked only now: while the switch above runs, a `switch name` must wait
            # for it rather than run a job on a GPU that isn't free yet.
            lock.mark(f"hold {name}")
            self.events.record("hold", mode=name, ok=True, previous=before, command=" ".join(command)[:200])
            try:
                proc = subprocess.Popen(command, env={**os.environ, HOLD_ENV: name})
            except OSError as e:
                self.events.record("hold-end", mode=name, ok=False, error=str(e))
                util.log(f"could not run {command[0]}: {e}")
                return 1
            # Pass termination on, so the lock is never released under a running render.
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, lambda signum, _frame: proc.send_signal(signum))
            code = proc.wait()
            self.events.record("hold-end", mode=name, exit=code)
            return code
