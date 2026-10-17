"""The arbiter: who owns the GPU, and handing it from one workload to another.

Every entry point goes through here:
  acquire  make room for a workload by releasing the others (used by switch and hold)
  gate     a workload's own pre-load hook asks whether it may load
  switch   acquire under the lock, from the CLI or the ComfyUI guard
  hold     acquire, then keep the lock until a command exits

Running work is never interrupted: a busy workload is waited for (up to `wait`)
or the request is refused. Ownership is derived from the live workloads each
time, never stored, so it can't drift.
"""

import os
import signal
import subprocess

from . import util
from .drivers.base import Workload
from .errors import ReleaseFailed, SwitchError
from .events import EventLog
from .lock import GpuLock, Held


class Arbiter:
    def __init__(self, workloads: list[Workload], gpu, host, lock: GpuLock, events: EventLog, *,
                 idle_owner: str | None = None, gate_release_timeout_s: float = 20,
                 release_timeout_s: float = 60, hold_env: str = "VHOTSEAT_HOLD"):
        self.workloads = {w.name: w for w in workloads}
        self.gpu = gpu
        self.host = host
        self.lock = lock
        self.events = events
        # The mode to report when no workload holds the GPU. The gpu-workload CLI
        # has always called that "comfy": ComfyUI may take it without a switch.
        self.idle_owner = idle_owner
        # A gate runs inside the workload's own load, which has a time limit
        # (Strata's before_load_timeout_s), so its releases get less time.
        self.gate_release_timeout_s = gate_release_timeout_s
        self.release_timeout_s = release_timeout_s
        # Set in a hold's command's environment, so a nested switch to the held
        # workload is a no-op instead of waiting on the lock the hold owns.
        self.hold_env = hold_env

    def snapshot(self) -> dict:
        return self.gpu.snapshot({n: w.proc for n, w in self.workloads.items()})

    def _others(self, name: str) -> list[Workload]:
        return [w for n, w in self.workloads.items() if n != name]

    # --- status --------------------------------------------------------------

    def probe(self) -> dict:
        gpu = self.snapshot()
        states = {n: w.probe(gpu) for n, w in self.workloads.items()}
        active = [n for n, w in self.workloads.items() if w.active(states[n], gpu)]
        mode = "conflict" if len(active) > 1 else active[0] if active else self.idle_owner
        ram = self.host.machine_ram()
        ram.update({n: self.host.memory(w.unit) for n, w in self.workloads.items()})
        return {"mode": mode, **states, "gpu": gpu, "ram": ram, "last_event": self.events.last()}

    def describe(self, s: dict) -> str:
        g = s["gpu"]
        lines = [f"mode:   {s['mode']}"]
        lines += [f"{n + ':':<8}{w.describe(s[n])}" for n, w in self.workloads.items()]
        shares = ", ".join(f"{n} {g[f'{n}_mib']}" for n in self.workloads)
        lines.append("gpu:    " + (g["error"] if "error" in g else
                                   f"{g['free_mib']} of {g['total_mib']} MiB free ({shares}, other {g['other_mib']})"))
        lines.append("ram:    " + self._describe_ram(s["ram"]))
        if ev := s.get("last_event"):
            detail = ", ".join(f"{k}={v}" for k, v in ev.items() if k not in ("time", "event"))
            lines.append(f"last:   {ev['time']} {ev['event']}" + (f" ({detail})" if detail else ""))
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

    def _make_room(self, others: list[Workload], wait: float, timeout: float, ensure: bool) -> list[str]:
        """Release every other workload; restart one whose release isn't enough,
        if it allows that. Returns what was done, for the event log."""
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
                    raise SwitchError(f"a {y.label} process holds VRAM but {y.unit}.service is stopped; "
                                      "kill it by hand")
            except SwitchError as e:
                e.workload = y
                raise
        return done

    def acquire(self, name: str, wait: float, load: bool) -> list[str]:
        """Give the GPU to `name`: release the others, start it if it's stopped,
        and load it if `load` and it can be loaded ahead of a request."""
        x = self.workloads[name]
        others = self._others(name)
        # A workload that may be loading invisibly is asked to release even when the
        # GPU looks like x's already, so this only short-cuts when none of them may be.
        if self.probe()["mode"] == name and not any(y.release_always for y in others):
            util.log(f"already in {name} mode")
            return []
        done = self._make_room(others, wait, self.release_timeout_s, ensure=True)
        x.ensure_running()
        loads = load and type(x).load is not Workload.load
        if loads:
            x.load(wait)
        if self.probe()["mode"] != name:
            what = (f"{x.label} reported loaded" if loads
                    else f"{', '.join(y.label for y in others)} unloaded")
            raise SwitchError(f"{what}, but the mode isn't {name}; run `gpu-workload status`")
        return done

    def gate(self, name: str) -> bool:
        """Run inside `name`'s own load (Strata's before_load): may it load?

        - A `switch name` or `hold name` holds the lock: this is the load it
          asked for, or it already gave the GPU to `name`. Allow at once.
          Waiting would deadlock: that switch is blocked on this very load.
        - Anything else holds the lock: refuse.
        - Otherwise a request woke the workload. Another workload busy, or its
          state unknown: refuse, a render is never interrupted. All idle:
          release them (restarting one if releasing isn't enough), then allow.

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

        with got:
            gpu = self.snapshot()
            reason = gpu.get("error") or next((r for y in others if (r := y.busy())), None)
            if not reason:
                try:
                    done = self._make_room(others, 0, self.gate_release_timeout_s, ensure=False)
                except SwitchError as e:
                    reason = f"could not make room in {e.workload.label}: {e}"
                else:
                    # What memory it loads into: oomd killed loads that skipped the restart.
                    ram = self.host.machine_ram()
                    self.events.record(kind, source="request", allowed=True,
                                       reason="; ".join([*(f"{y.label} idle" for y in others), *done]),
                                       available_mib=ram["available_mib"],
                                       **{y.name: self.host.memory(y.unit) for y in others})
                    return True
            self.events.record(kind, source="request", allowed=False, reason=reason)
            util.log(f"refusing {x.label} load: {reason}")
            return False

    def _take(self, name: str, *, skip_if_held: bool):
        def waiting(_holder):
            util.log("waiting for another switch or hold to finish")
        unless = (lambda h: h == f"hold {name}") if skip_if_held else None
        return self.lock.take(f"switch {name}", unless=unless, on_wait=waiting)

    def switch(self, name: str, wait: float) -> dict:
        """Move the GPU to `name` under the lock. Returns {"ok": …} plus "held"
        or "error"; the caller prints status.

        While a `hold name` owns the lock, the GPU is already `name`'s and stays
        so: return at once. That's how a ComfyUI job queued by a held script gets
        past its guard; before, the guard waited on the hold, which was waiting
        on the job."""
        held = os.environ.get(self.hold_env)
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
                self.acquire(name, wait, load=True)
            except SwitchError as e:
                self.events.record("switch", to=name, ok=False, error=str(e))
                util.log(f"switch to {name} failed: {e}")
                return {"ok": False, "error": str(e)}
        if before != name:
            self.events.record("switch", to=name, ok=True, previous=before)
        return {"ok": True}

    def hold(self, name: str, command: list[str], wait: float) -> int:
        """Switch to `name` and keep the GPU there until `command` exits; returns
        its exit code. The lock is marked "hold name" once the switch is done, so
        every other workload's gate refuses, and a script may leave the queue
        empty between its jobs."""
        if os.environ.get(self.hold_env) == name:
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
                proc = subprocess.Popen(command, env={**os.environ, self.hold_env: name})
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
