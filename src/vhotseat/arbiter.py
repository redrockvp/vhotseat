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

from . import claims, host, util
from .drivers.base import Workload
from .errors import Busy, ReleaseFailed, SwitchError
from .events import EventLog
from .lock import GpuLock, Held

NONE = "none"
# Set in a hold's command's environment, so a nested switch to the held workload
# is a no-op instead of waiting on the lock the hold owns.
HOLD_ENV = "VHOTSEAT_HOLD"


class Arbiter:
    def __init__(self, workloads: list[Workload], gpu, lock: GpuLock, events: EventLog, *,
                 gate_release_timeout_s: float = 20, release_timeout_s: float = 60,
                 machine_ram=host.machine_ram, telemetry=None, power: dict | None = None,
                 claim_rules: list | None = None):
        self.workloads = {w.name: w for w in workloads}
        self.gpu = gpu
        self.lock = lock
        self.events = events
        # A gate runs inside the workload's own load, which has a time limit
        # (Strata's before_load_timeout_s), so its releases get less time.
        self.gate_release_timeout_s = gate_release_timeout_s
        self.release_timeout_s = release_timeout_s
        self.machine_ram = machine_ram
        # Every probe is kept as a sample, for `vhotseat web`'s history.
        self.telemetry = telemetry
        # What a unit of electricity costs where this machine is (config `power:`).
        self.power = power or {}
        # What owns the card by right rather than by arrangement (a game): see claims.py.
        self.claim_rules = claim_rules if claim_rules is not None else claims.rules([])

    def snapshot(self) -> dict:
        return self.gpu.snapshot({n: w.vram_selector for n, w in self.workloads.items()})

    def snapshot_all(self) -> tuple[dict, list]:
        """One read of the card: the attribution sums and the named processes behind them.
        A backend that can't name processes gives the sums and an empty list."""
        sel = {n: w.vram_selector for n, w in self.workloads.items()}
        fn = getattr(self.gpu, "read_all", None)
        return fn(sel) if fn else (self.gpu.snapshot(sel), [])

    def holders(self, limit: int = 8) -> list[dict]:
        """Who holds the card's memory, biggest first, by name: the workloads vhotseat
        manages and, with workload None, the ones nothing manages (a desktop, a game)."""
        return self.snapshot_all()[1][:limit]

    def gpu_health(self) -> dict:
        """The card's model, load, temperature and power; not part of `status`."""
        return self.gpu.health() if hasattr(self.gpu, "health") else {}

    def _others(self, name: str | None) -> list[Workload]:
        return [w for n, w in self.workloads.items() if n != name]

    # --- status --------------------------------------------------------------

    def probe(self) -> dict:
        gpu, held = self.snapshot_all()
        states = {n: w.probe(gpu) for n, w in self.workloads.items()}
        active = [n for n, w in self.workloads.items() if w.active(states[n], gpu)]
        mode = "conflict" if len(active) > 1 else active[0] if active else NONE
        ram = self.machine_ram()
        ram.update({n: w.memory() for n, w in self.workloads.items()})
        status = {"mode": mode, **states, "gpu": gpu, "holders": held, "claims": self._claims_in(held),
                  "ram": ram, "last_event": self.events.last()}
        if self.telemetry is not None:
            self.telemetry.sample(status, self.gpu_health)
        return status

    def describe(self, s: dict) -> str:
        g = s["gpu"]
        width = max(len(n) for n in [*self.workloads, "mode"]) + 2
        lines = [f"{'mode:':<{width}}{s['mode']}"]
        lines += [f"{n + ':':<{width}}{w.describe(s[n])}" for n, w in self.workloads.items()]
        shares = ", ".join(f"{n} {g[f'{n}_mib']}" for n in self.workloads)
        lines.append(f"{'gpu:':<{width}}" + (g["error"] if "error" in g else
                     f"{g['free_mib']} of {g['total_mib']} MiB free ({shares}, other {g['other_mib']})"))
        if rows := s.get("holders"):
            shown = "  ".join(f"{r['name'] or r['pid']} {r['mib']}" + (f" ({r['workload']})" if r["workload"] else "")
                              for r in rows[:5])
            lines.append(f"{'holds:':<{width}}{shown}")
        if hits := s.get("claims"):
            lines.append(f"{'claims:':<{width}}the card is claimed by {claims.describe(hits)}: nothing new "
                         "loads while it runs")
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
            snap = self.snapshot()
            have = snap.get("free_mib")
            # What x already has on the card is not room it still has to find: a model that
            # is loaded, or landing right now, has paid for itself. Without this, a second
            # request arriving while the first one's weights stream in reads the target's own
            # 20 GB as the blocker and refuses the very load that put it there.
            if have is not None:
                have += max(0, x.vram_mib() - x.idle_vram_mib)
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
                    # "Closed by hand must not block the others" and "a server that is still
                    # coming up must not be walked over" are the same question: is anything there?
                    # Nothing listening and nothing on the card means it is gone, and release()
                    # judges from the GPU counters. Anything else gets the wait: a llama-server
                    # mid-load answers 503, and a 2 s reachable() probe reads that as dead.
                    if ensure and (y.listening() or y.vram_mib() > y.idle_vram_mib):
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

    def _claims_in(self, held: list[dict]) -> list[dict]:
        owned = {r["pid"] for r in held if r["workload"]}
        return claims.matching(self.claim_rules, host.processes(), owned)

    def claim_hits(self) -> list[dict]:
        """Processes that own the card right now - a game, or whatever a `claims:` rule
        matches. Never a pid already credited to a workload: the thing holding the card
        for vhotseat is not also the thing that owns it. Cheap enough to ask at every
        decision (a machine-wide scan is tens of milliseconds), which is what keeps
        vhotseat stateless: no daemon has to notice a game for the rule to hold."""
        return self._claims_in(self.snapshot_all()[1])

    def why_all(self) -> dict:
        """For each workload: could it take the card right now, and what stands in the
        way. It reads the same state `switch` would act on and touches nothing - no
        release, no load, no restart. What it's for is the question a dashboard can't
        otherwise answer: not who holds the card, but why the next job can't have it."""
        s = self.probe()
        gpu, ram = s["gpu"], s["ram"]
        hits = self.claim_hits()
        out = {}
        for name, x in self.workloads.items():
            notes: list[str] = []
            would, freed = "ready", 0
            resident = x.active(s[name], gpu)
            if s["mode"] == name:
                notes.append("it holds the card now")
            if s["mode"] == "conflict":
                notes.append("two workloads are on the card at once: asking for it will release the other")
            for y in self._others(name):
                held = y.vram_mib()
                if not y.service_active():
                    if held > y.idle_vram_mib:
                        would, why = "blocked", f"a {y.label} process holds {held} MiB but its service is stopped"
                        notes.append(f"{why}: kill it by hand")
                    continue
                if not y.reachable():
                    if held > y.idle_vram_mib:
                        would = "blocked"
                        notes.append(f"{y.label} isn't answering and still holds {held} MiB: start it so "
                                     "vhotseat can release it, or kill the process")
                    else:
                        notes.append(f"{y.label} isn't answering and holds nothing on the card: treated as stopped")
                    continue
                if busy := y.busy():
                    if would == "ready":
                        would = "waiting"
                    notes.append(f"{y.label} is busy: {busy}; a handover waits for it to finish")
                elif held > y.idle_vram_mib:
                    freed += held
                    notes.append(f"{y.label} holds {held} MiB and would be asked to release it")
            adm, model = x.admission_for()
            who = f"{x.label} ({model})" if model else x.label
            if hits:
                if resident:
                    notes.append(f"the card is claimed by {claims.describe(hits)}: it keeps what it has, "
                                 "but reloading it would be refused")
                else:
                    would, why = "blocked", (f"the card is claimed by {claims.describe(hits)}: "
                                             "nothing new loads while it runs")
                    notes.insert(0, why)
            if (need := (adm or {}).get("min_free_vram_mib")) and not resident:
                free = gpu.get("free_mib") or 0
                have = free + freed
                if have < need:
                    would = "blocked"
                    notes.append(f"{who} only loads with {need} MiB free: releasing the others gets {have} MiB "
                                 f"({free} free now)")
                    if gpu.get("other_mib"):
                        unmanaged = ", ".join(f"{r['name'] or r['pid']} {r['mib']}" for r in s["holders"]
                                              if not r["workload"])[:160]
                        notes.append(f"{gpu['other_mib']} MiB is held by processes vhotseat doesn't manage"
                                     + (f" ({unmanaged})" if unmanaged else "") + ": nothing here frees it")
            if (need := (adm or {}).get("min_available_ram_mib")) and not resident:
                have = ram.get("available_mib")
                if have is not None and have < need:
                    if would == "ready":
                        would = "waiting"
                    notes.append(f"{who} wants {need} MiB of available RAM, {have} MiB is free; getting it back "
                                 "from a workload means restarting it")
            sized = (s[name] or {}).get("sized") or {}
            if sized.get("bytes"):
                # Reported, not enforced: the server may still fit it by shrinking its context
                # or spilling into RAM. The number is for seeing that before the weights land.
                note = (f"{(sized.get('model') or x.label).rsplit('/', 1)[-1]} sizes at "
                        f"{sized['bytes'] // 2**20} MiB")
                if sized.get("n_ctx"):
                    note += f" at {sized['n_ctx']} ctx"
                card = gpu.get("total_mib")
                if card and sized["bytes"] // 2**20 > card:
                    note += f", more than the card's {card} MiB: only part of it can be resident"
                notes.append(note)
            out[name] = {"target": name, "label": x.label, "would": would, "holder": s["mode"],
                         "free_mib": gpu.get("free_mib"), "resident": resident, "notes": notes}
        return out

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
            hits = self.claim_hits()
            reason = (gpu.get("error")
                      or (f"the card is claimed by {claims.describe(hits)}: nothing new loads while it runs"
                          if hits else None)
                      or next((r for y in others if (r := y.busy())), None))
            if not reason:
                # Already loaded (a hook fired again, or `vhotseat gate` run by hand):
                # its own memory isn't a shortfall, so don't restart anyone for it.
                resident = x.active(x.probe(gpu), gpu)
                try:
                    done = self._make_room(x, 0, self.gate_release_timeout_s, ensure=False, deadline=deadline,
                                           admit=not resident)
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

    def _busy_other(self, name: str) -> str | None:
        """Why another workload mustn't be released now, if one says it's working."""
        return next((r for y in self._others(name) if y.service_active() and (r := y.busy())), None)

    def _wait_idle(self, name: str, deadline: float) -> None:
        """Wait, without the lock, until no other workload says it's working, or raise
        Busy at the deadline. Workloads that only learn they're busy when asked to
        release (Strata's 409) say nothing here; release() waits for those."""
        said = False
        while (reason := self._busy_other(name)):
            if time.monotonic() >= deadline:
                raise Busy(reason + " (use --wait SECONDS to wait for it)" if not said else
                           f"timed out waiting: {reason}")
            if not said:
                util.log(f"waiting: {reason}")
                said = True
            util.sleep(1.0)

    def _take(self, name: str, *, skip_if_held: bool):
        def waiting(_holder):
            util.log("waiting for another switch or hold to finish")
        unless = (lambda h: h == f"hold {name}") if skip_if_held else None
        return self.lock.take(f"switch {name}", unless=unless, on_wait=waiting)

    def switch(self, name: str, wait: float, load: bool = True, request: bool = False) -> dict:
        """Move the GPU to `name` (or free it, for "none") under the lock. Returns
        {"ok": …} plus "held" or "error"; the caller prints status.

        While a `hold name` owns the lock, the GPU is already `name`'s and stays
        so: return at once. That's how a ComfyUI job queued by a held script gets
        past its guard; before, the guard waited on the hold, which was waiting
        on the job.

        `request` marks a switch a workload asked for on somebody's behalf - ComfyUI's
        guard node - rather than one a person typed. A claimed process refuses the
        request and not the typed command: "don't load on top of the game" is a rule
        about loads nobody chose, and someone who types `vhotseat switch` has already
        decided. A request that changes nothing (the workload already holds the card,
        so nothing new would load) is not refused either: a batch mid-flight carries on,
        which is the same promise the guard makes to a render in flight."""
        held = os.environ.get(HOLD_ENV)
        if held:
            # Inside `hold`: our parent owns the lock, so taking it would deadlock.
            if held != name:
                util.log(f"can't switch to {name} inside `hold {held}`")
                return {"ok": False, "error": f"inside hold {held}", "inside_hold": held}
            return {"ok": True, "held": held}
        if request and (hits := self.claim_hits()) and self.probe()["mode"] != name:
            why = f"the card is claimed by {claims.describe(hits)}: nothing new loads while it runs"
            self.events.record("switch", to=name, ok=False, error=why)
            util.log(f"switch to {name} refused: {why}")
            return {"ok": False, "error": why}
        # Wait for the others' running work *without* the lock: a ComfyUI job's guard
        # needs the lock to start, so holding it while waiting for ComfyUI's queue
        # to drain would stop the queue (it did, until 2026-10-17: the batch stalled
        # for the whole wait, then the switch failed). Under the lock, release
        # without waiting; if work slipped in meanwhile, let go and wait again.
        deadline = time.monotonic() + wait * util.SCALE
        while True:
            try:
                self._wait_idle(name, deadline)
            except SwitchError as e:
                self.events.record("switch", to=name, ok=False, error=str(e))
                util.log(f"switch to {name} failed: {e}")
                return {"ok": False, "error": str(e), "exception": e}
            lock = self._take(name, skip_if_held=True)
            if lock is None:
                return {"ok": True, "held": name}
            with lock:
                # Work that started between the wait and the lock: wait again outside.
                if self._busy_other(name) and time.monotonic() < deadline:
                    continue
                left = max(0.0, (deadline - time.monotonic()) / util.SCALE)
                before = self.probe()["mode"]
                try:
                    done = self.acquire(name, left, load=load)
                except SwitchError as e:
                    self.events.record("switch", to=name, ok=False, error=str(e))
                    util.log(f"switch to {name} failed: {e}")
                    return {"ok": False, "error": str(e), "exception": e}
            break
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
            if os.name == "nt":
                # The console sends Ctrl+C / Ctrl+Break to the command too: just wait for it.
                # A SIGTERM can only be passed on as TerminateProcess.
                for sig in (signal.SIGINT, signal.SIGBREAK):
                    signal.signal(sig, lambda _signum, _frame: None)
                signal.signal(signal.SIGTERM, lambda _signum, _frame: proc.terminate())
            else:
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    signal.signal(sig, lambda signum, _frame: proc.send_signal(signum))
            code = proc.wait()
            self.events.record("hold-end", mode=name, exit=code)
            return code
