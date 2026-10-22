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

import datetime
import os
import signal
import subprocess
import time

from . import claims, host, policy, pressure, util
from .drivers.base import Workload
from .errors import Busy, PolicyRefused, ReleaseFailed, SwitchError
from .events import EventLog, row_time
from .lock import GpuLock, Held

NONE = "none"


def _declined(why: str, by_actor: bool = False) -> dict:
    """A pressure call that did nothing answers in the same shape as one that did: whoever asked
    can read `released` without knowing which of four ways out was taken."""
    return {"declined": why, "by_actor": by_actor, "episode": None, "released": [],
            "waiting": [], "freed_mib": 0, "would": []}
# Set in a hold's command's environment, so a nested switch to the held workload
# is a no-op instead of waiting on the lock the hold owns.
HOLD_ENV = "VHOTSEAT_HOLD"


class Arbiter:
    def __init__(self, workloads: list[Workload], gpu, lock: GpuLock, events: EventLog, *,
                 gate_release_timeout_s: float = 20, release_timeout_s: float = 60,
                 machine_ram=host.machine_ram, telemetry=None, power: dict | None = None,
                 claim_rules: list | None = None, claim_ignore=(), claim_poll_s: float = 2.0,
                 pressure_cfg: dict | None = None):
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
        # Names and path globs the built-in rules won't claim (Wallpaper Engine, a VR runtime).
        self.claim_ignore = claims.ignores(claim_ignore)
        # How often the watcher looks for a claim. A game allocates within seconds of starting,
        # so this is faster than the telemetry sampling and keeps its own clock (config
        # `claims_poll_s`). The refusal doesn't depend on it - every gate and guard checks for
        # itself - only the release and the record do.
        self.claim_poll_s = claim_poll_s
        # The pressure trigger: an app vhotseat doesn't manage taking the card. `pressure_cfg` is
        # the config block with the measured defaults in it; `pressure_ignores()` reads its
        # `ignore:` - the processes the owner has said are never the reason (a VM, a benchmark, a
        # miner), whose memory is out of the measurement rather than just out of the report.
        self.pressure_cfg = pressure.cfg(pressure_cfg)

    def snapshot(self) -> dict:
        return self.gpu.snapshot({n: w.vram_selector for n, w in self.workloads.items()})

    def snapshot_all(self, limit: int = 8) -> tuple[dict, list]:
        """One read of the card: the attribution sums and the named processes behind them.
        A backend that can't name processes gives the sums and an empty list. `limit` is what a
        report wants; 0 is every process, which is what the pressure test needs - the app that grew
        is not necessarily among the eight biggest holders."""
        sel = {n: w.vram_selector for n, w in self.workloads.items()}
        fn = getattr(self.gpu, "read_all", None)
        return fn(sel, limit) if fn else (self.gpu.snapshot(sel), [])

    def holders(self, limit: int = 8) -> list[dict]:
        """Who holds the card's memory, biggest first, by name: the workloads vhotseat
        manages and, with workload None, the ones nothing manages (a desktop, a game)."""
        return self.snapshot_all()[1][:limit]

    def gpu_health(self) -> dict:
        """The card's model, load, temperature and power; not part of `status`."""
        return self.gpu.health() if hasattr(self.gpu, "health") else {}

    def _others(self, name: str | None) -> list[Workload]:
        return [w for n, w in self.workloads.items() if n != name]

    def label(self, name: str) -> str:
        """A workload's label, or the name itself for anything that isn't a workload."""
        return self.workloads[name].label if name in self.workloads else name

    # --- who may ask a workload to let go of its models --------------------------

    def loaded(self, gpu: dict) -> list[str]:
        """Which workloads hold model memory in this snapshot. A workload at or under its
        idle weight has nothing on the card that anyone could be short of, so it refuses
        nobody whatever its policy says; that is also what keeps an idle server with a
        narrow `yields_to:` from blocking the card it isn't holding."""
        return [n for n, w in self.workloads.items()
                if (gpu.get(f"{n}_mib") or 0) > w.idle_vram_mib]

    def wont_give_way(self, reason: str, gpu: dict) -> list[str]:
        """The loaded workloads whose `yields_to:` doesn't cover `reason` - a workload's
        name, `claims` or `pressure` (policy.py)."""
        return [n for n in self.loaded(gpu)
                if n != reason and not policy.yields(self.workloads[n].yields_to, reason)]

    def policy_why(self, requester: str, blockers: list[str], gpu: dict) -> str:
        """The refusal in words a person can act on: who is in the way, how much it is
        holding, and what it does give way for - which is the key to change."""
        who = self.label(requester)
        bits = [f"{self.label(n)} holds {gpu.get(f'{n}_mib')} MiB and its yields_to "
                f"({policy.describe(self.workloads[n].yields_to)}) doesn't cover {who}"
                for n in blockers]
        return f"nothing will make room for {who}: " + "; ".join(bits)

    def kept_for_policy(self, reason: str, gpu: dict | None = None) -> list[dict]:
        """The loaded workloads that won't give way for a release (`claims`, later
        `pressure`). A release is never refused, so these keep holding: whoever asked for
        the card may now be short of exactly that much, which is why each one is worth a row
        in the log and a line on the dashboard rather than silence."""
        gpu = self.snapshot() if gpu is None else gpu
        return [{"workload": n, "held_mib": gpu.get(f"{n}_mib") or 0}
                for n in self.wont_give_way(reason, gpu)]

    def kept_why(self, reason: str, name: str, held_mib: int) -> str:
        """Why something stayed loaded, in the sentence the row and the banner show."""
        return (f"{self.label(name)} holds {held_mib} MiB and its yields_to "
                f"({policy.describe(self.workloads[name].yields_to)}) doesn't cover "
                f"{policy.PHRASE.get(reason, reason)}: it keeps its model, and whatever "
                f"asked for the card may be short of that")

    def _policy_refused(self, source: str, requester: str, blockers: list[str], why: str) -> None:
        """One row for one refusal, so the dashboard can warn about it and `why` and
        `/api/events` can explain it after the fact. `ok: false` is what marks it red on
        the charts, where the load it stopped is the point."""
        self.events.record("policy-refused", source=source, requested=requester, ok=False,
                           workloads=blockers, why=why)

    # --- status --------------------------------------------------------------

    def probe(self) -> dict:
        # One card read, every process in it: the pressure reading needs the app that grew, which
        # is not necessarily among the eight biggest holders, and the report has to be built from
        # the same numbers the trigger would act on.
        gpu, every = self.snapshot_all(limit=0)
        held = every[:8]
        states = {n: w.probe(gpu) for n, w in self.workloads.items()}
        active = [n for n, w in self.workloads.items() if w.active(states[n], gpu)]
        mode = "conflict" if len(active) > 1 else active[0] if active else NONE
        ram = self.machine_ram()
        ram.update({n: w.memory() for n, w in self.workloads.items()})
        hits = self._claims_in(held)
        status = {"mode": mode, **states, "gpu": gpu, "holders": held, "claims": hits,
                  "policy": {n: {"priority": w.priority, "yields_to": list(w.yields_to),
                                  "gives_way_for": policy.describe(w.yields_to)}
                             for n, w in self.workloads.items()},
                  "pressure": self.pressure_status(card=(gpu, every)),
                  "ram": ram, "last_event": self.events.last()}
        if hits:
            # And what has been done about it: what this claim has already been given, and what
            # is still holding. Read back from the event log, so it costs nothing when nothing
            # claims the card and survives whichever process did the releasing.
            status["claims_state"] = self.events.claims_open()
        if self.telemetry is not None:
            self.telemetry.sample(status, self.gpu_health)
        return status

    def describe(self, s: dict) -> str:
        g = s["gpu"]
        width = max(len(n) for n in [*self.workloads, "mode"]) + 2

        def pad(label: str) -> str:
            """A column label, padded to the column - and never narrower than itself. The
            column is a workload name wide, and `freed for:` is longer than most: the label
            used to run into the process behind it, on the one line that says what a claim
            was given."""
            return f"{label:<{max(width, len(label) + 1)}}"

        lines = [f"{pad('mode:')}{s['mode']}"]
        lines += [f"{pad(n + ':')}{w.describe(s[n])}" for n, w in self.workloads.items()]
        # Who will make room for whom, and in what order: the two keys are the owner's, and
        # a refusal that doesn't name them is unfixable from the outside.
        lines.append(f"{pad('policy:')}" + "; ".join(
            f"{n} {w.priority} ({policy.describe(w.yields_to)})" for n, w in self.workloads.items()))
        shares = ", ".join(f"{n} {g[f'{n}_mib']}" for n in self.workloads)
        lines.append(f"{pad('gpu:')}" + (g["error"] if "error" in g else
                     f"{g['free_mib']} of {g['total_mib']} MiB free ({shares}, other {g['other_mib']})"))
        if rows := s.get("holders"):
            shown = "  ".join(f"{r['name'] or r['pid']} {r['mib']}" + (f" ({r['workload']})" if r["workload"] else "")
                              for r in rows[:5])
            lines.append(f"{pad('holds:')}{shown}")
        if hits := s.get("claims"):
            free = any(h.get("on_start", "release") == "release" for h in hits)
            lines.append(f"{pad('claims:')}the card is claimed by {claims.describe(hits)}: "
                         + ("unloading what is loaded to free it, and nothing new loads while it "
                            "runs" if free else "nothing new loads while it runs"))
            hit_keys = {(h["name"], h["note"]) for h in hits}
            for key, rec in sorted((s.get("claims_state") or {}).items()):
                if key not in hit_keys:
                    continue
                bits = []
                if rec["released"]:
                    bits.append("unloaded " + ", ".join(rec["released"])
                                + (f" ({rec['freed_mib']} MiB)" if rec.get("freed_mib") else ""))
                for w in rec["waiting"]:
                    bits.append(f"{w['workload']} keeps its model: {w['why']}")
                if kept := rec.get("kept"):
                    bits.append("kept for its policy: " + ", ".join(kept))
                if bits:
                    lines.append(f"{pad('freed for:')}{key[0]} ({key[1]}) - " + "; ".join(bits))
        pr = s.get("pressure") or {}
        if open := pr.get("open"):
            # The card is short right now for something vhotseat doesn't manage, and this says so in
            # the same words the release wrote: what took the memory, how far past its usual it is,
            # and what has come off and what is still holding because of it.
            bits = [pressure.describe(open)]
            if open.get("released"):
                bits.append("unloaded " + ", ".join(self.label(n) for n in open["released"])
                            + (f" ({open['freed_mib']} MiB)" if open.get("freed_mib") else ""))
            for w in open.get("waiting") or []:
                bits.append(f"{self.label(w['workload'])} keeps its model: {w['why']}")
            lines.append(f"{pad('pressure:')}" + "; ".join(bits))
        lines.append(f"{pad('ram:')}" + self._describe_ram(s["ram"]))
        if ev := s.get("last_event"):
            detail = ", ".join(f"{k}={v}" for k, v in ev.items() if k not in ("time", "event"))
            lines.append(f"{pad('last:')}{ev['time']} {ev['event']}" + (f" ({detail})" if detail else ""))
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
                   deadline: float | None = None, admit: bool = True, requested: bool = False,
                   gpu: dict | None = None) -> list[str]:
        """Release every workload but `x`, restarting one whose release isn't
        enough when it allows that; then, if `admit`, restart idle ones while
        `x`'s admission isn't met. Returns what was done, for the event log.

        Admission is for a workload about to load: one already holding the GPU
        would count its own memory against itself.

        `requested` marks a handover nobody typed a decision about: Strata's gate, a
        ComfyUI job's guard, a proxy's Seat. For those, a workload holding the card that
        doesn't give way for `x` makes the whole handover a refusal (PolicyRefused) rather
        than a partial one, because the card can't be given to `x` without it. A `switch`
        or `hold` a person typed is an order, and unloads it anyway: that is the promise
        made everywhere else, and the log says whose policy was overridden.
        """
        gpu = self.snapshot() if gpu is None else gpu
        if x is not None:
            blockers = self.wont_give_way(x.name, gpu)
            if blockers:
                why = self.policy_why(x.name, blockers, gpu)
                if requested:
                    raise PolicyRefused(why, workloads=blockers)
                self.events.record("policy-override", requested=x.name, workloads=blockers,
                                   why=why)
                util.log(f"switch to {self.label(x.name)} overrides a policy: {why}")
        # Lowest priority first; a stable sort keeps the config's order among equals, which
        # is what it was before there were priorities at all.
        others = sorted(self._others(x.name if x else None), key=lambda w: w.priority)
        done = []
        for y in others:
            try:
                if y.service_active():
                    # "A server still coming up must not be walked over" and "a workload closed by
                    # hand must not block the others" are the same question: is there something to
                    # bring up? Holding its port, or holding no more than its idle weight, yes: it
                    # gets the chance to come up and unload politely. Answering nothing *while*
                    # holding the card is the other case - a process that died with its VRAM stuck -
                    # and starting a program can't free that memory, so there it goes straight to
                    # the wait below, which says so by MiB and names what to do instead.
                    if ensure and (y.listening() or y.vram_mib() <= y.idle_vram_mib):
                        try:
                            y.ensure_running(timeout=min(self.release_timeout_s, max(1.0, timeout)))
                        except Exception as exc:
                            util.log(f"{y.name}: can't be started to unload from ({exc}); "
                                     "waiting for its GPU memory to drop")
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

    def acquire(self, name: str, wait: float, load: bool, requested: bool = False) -> list[str]:
        """Give the GPU to `name` (or to nobody, for "none"): release the others,
        start it if it's stopped, check its admission, and load it if `load`.
        `requested` is what a workload asked for on nobody's order (see _make_room)."""
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
        done = self._make_room(x, wait, self.release_timeout_s, ensure=True, admit=not resident,
                               requested=requested, gpu=s["gpu"])
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
        procs = host.processes()
        # Every pid of a configured workload, not just the ones holding enough memory to make
        # the holder list. A workload installed under D:\Games\ matches the built-in rule for a
        # game in a Games folder: while it is idle it isn't in that list, and it would then
        # claim the card against its own loads.
        owned = {r["pid"] for r in held if r["workload"]}
        for w in self.workloads.values():
            sel = w.vram_selector
            if not sel or sel[0] != "process":
                continue
            needle = sel[1].lower()
            for pid, p in procs.items():
                if needle in f"{p.get('exe') or ''} {p.get('name') or ''}".lower():
                    owned.add(pid)
        return claims.matching(self.claim_rules, procs, owned, self.claim_ignore)

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
        otherwise answer: not who holds the card, but why the next job can't have it.

        The policy is part of that state: a workload that won't give way for the one being
        asked about is a blocker, and one the claim won't unload is named, because "the game
        is short of VRAM" is otherwise a mystery. Each answer carries its own priority and
        yields_to, so what to change is on the same line as what is wrong.
        """
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
                if hits and held > y.idle_vram_mib and not policy.yields(y.yields_to, policy.CLAIMS):
                    # The claim will leave this one alone, so the game gets less than the
                    # card could give it. Worth saying on the workload being asked about:
                    # "the game is short of VRAM" is otherwise a mystery.
                    notes.append(f"{y.label} keeps its model for the claim: its yields_to "
                                 f"({policy.describe(y.yields_to)}) doesn't cover a claim on the card")
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
                    if not policy.yields(y.yields_to, name):
                        would = "blocked"
                        notes.append(f"{y.label} holds {held} MiB and won't give way for {x.label}: "
                                     f"its yields_to ({policy.describe(y.yields_to)}) doesn't cover it")
                    else:
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
            # An episode that is open now, or ran and ended, is part of why a workload is off the
            # card and why nothing is putting it back: an owner who sees Strata unloaded ten minutes
            # after a game closed needs to know which of the two did it.
            if (ep := (s.get("pressure") or {}).get("open")):
                row = ep["episode"]
                if name in (ep.get("released") or []):
                    notes.append(f"{row['time']}: its model came off for the pressure episode "
                                 f"({row.get('name')} grew to {row.get('grew_mib')} MiB): vhotseat "
                                 "won't chase it back on, reloading is your call")
                elif name in (ep.get("targets") or []):
                    notes.append(f"the pressure episode for {row.get('name')} is still owed this "
                                 "workload: it has not come off yet")
                elif not policy.yields(x.yields_to, policy.PRESSURE):
                    notes.append(f"it was never a pressure target: its yields_to "
                                 f"({policy.describe(x.yields_to)}) doesn't cover pressure")
                if not x.active(x.probe(gpu), gpu):
                    notes.append("nothing loads it back while the card is short this way: only "
                                 "loading something, or `switch`, is the order to fill it again")
            elif (last := (s.get("pressure") or {}).get("last")) and last.get("event") == "pressure-end":
                notes.append(f"{last['time']}: the pressure episode for {last.get('name')} ended "
                             f"({last.get('reason')}): what it unloaded stayed unloaded")
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
                         "free_mib": gpu.get("free_mib"), "resident": resident,
                         "priority": x.priority, "yields_to": list(x.yields_to), "notes": notes}
        return out

    def gate(self, name: str) -> bool:
        """Run inside `name`'s own load (Strata's before_load): may it load?

        - A `switch name` or `hold name` holds the lock: this is the load it
          asked for, or it already gave the GPU to `name`. Allow at once.
          Waiting would deadlock: that switch is blocked on this very load.
        - Anything else holds the lock: refuse.
        - Otherwise a request woke the workload. Another workload busy, or its
          state unknown: refuse, a render is never interrupted. Another workload
          holding the card that won't give way for `name` (its `yields_to:`): refuse,
          and name it. All idle and willing: release them (restarting one if releasing
          isn't enough), check admission, then allow.

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
                    done = self._make_room(x, 0, self.gate_release_timeout_s, ensure=False,
                                           deadline=deadline, admit=not resident, requested=True,
                                           gpu=gpu)
                    self.lock.bump()
                except PolicyRefused as e:
                    self._policy_refused("gate", x.name, e.workloads, str(e))
                    reason = str(e)
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

    def claim_targets(self) -> list[str]:
        """Which workloads a claim is owed: the ones holding more than their idle VRAM on the
        card right now, lowest priority first.

        A workload whose `yields_to:` doesn't cover `claims` isn't on that list whatever it
        holds: the game is short of that much VRAM and vhotseat says so rather than unloading
        something the owner refused to move for a game. It is recorded as kept
        (`policy-kept`), not as waiting - nothing is waiting for it, it was never owed.

        This is decided once, when the claim's release first runs, and the answer goes in the
        event log (`claim-release-targets`). It matters because a claim lasts as long as the
        game does, and a typed `vhotseat switch strata` in the middle of it is an order: what
        the owner loads afterwards is theirs, and a watcher that read the card fresh every poll
        would undo it every 2 seconds for the rest of the game."""
        snap = self.snapshot()
        owed = [n for n, y in self.workloads.items()
                if (snap.get(f"{n}_mib") or 0) > y.idle_vram_mib
                and policy.yields(y.yields_to, policy.CLAIMS)]
        return sorted(owed, key=lambda n: (self.workloads[n].priority, n))

    def claim_release(self, claim: dict, only: list[str] | None = None) -> dict:
        """A claim appeared (`on_start: release`): ask the workloads it was owed to let go of
        the card, through the normal release path - API unload, then the card confirms the MiB
        went - and leave anything that is working alone.

        `only` is that set, read back from the log by whoever is acting; workloads outside it
        are not touched for this claim whatever they come to hold later. With no set passed,
        every workload holding card memory is a candidate.

        It takes the GPU lock exactly as a switch would: a `hold` or a switch in progress owns
        the card, and releasing underneath it would pull the ground out from under that work, so
        this declines and the watcher asks again on the next poll. The claim it is serving never
        refuses this: freeing the card is not a load (see api.Seat._switch and switch's `request`).

        Nothing is restarted and nothing is killed. A workload whose release doesn't finish -
        busy, refusing, or stuck holding its VRAM - is reported as waiting, and the watcher tries
        it again while the claim lasts, bounded for a workload that says it unloaded and keeps
        holding. A workload with nothing on the card isn't touched at all: a model living
        entirely in RAM has nothing here that the game is short of, and neither is one whose
        `yields_to:` doesn't cover `claims` - that one was never a target, and isn't asked.

        Returns {"declined": why-or-None, "released": [{workload, freed_mib}],
        "waiting": [{workload, why, stuck?}], "freed_mib": int}.
        """
        cands = self.workloads if only is None else {n: y for n, y in self.workloads.items()
                                                    if n in set(only)}
        # A workload that doesn't give way for a claim is never owed to one, even if the list
        # recorded when the claim started says otherwise: the list is what the card held then,
        # this is the config's answer now (an owner who protects a workload mid-game stops the
        # watcher coming back for it on the next poll).
        cands = {n: y for n, y in cands.items() if policy.yields(y.yields_to, policy.CLAIMS)}
        pre = self.snapshot()
        if not any((pre.get(f"{n}_mib") or 0) > y.idle_vram_mib for n, y in cands.items()):
            # Nothing it is owed is on the card, so there is nothing to free and no reason to
            # take the lock every poll for as long as the game runs. `would` says as much.
            return {"declined": None, "by_actor": False, "would": [], "released": [],
                    "waiting": [], "freed_mib": 0}
        got = self.lock.try_take(f"claim release {claim.get('name') or ''}".strip())
        if not isinstance(got, Held):
            want = {n: max(0, (pre.get(f"{n}_mib") or 0) - y.idle_vram_mib)
                    for n, y in cands.items()}
            # Another watcher (watch and the dashboard both act) is mid-release for this same
            # claim. That isn't a workload holding on: saying so once per workload would both
            # spam the log and shadow the reason that matters, if one turns up later.
            sister = bool(got and got.startswith("claim release"))
            return {"declined": f"{got or 'a switch'} in progress", "by_actor": sister,
                    "would": sorted(n for n, mib in want.items() if mib),
                    "released": [], "waiting": [], "freed_mib": 0}
        with got:
            # Read the card again now that the lock is ours. Whoever was waiting behind this
            # lock - the other poller, on a box where watch and the dashboard both run - sees
            # the card as the first one left it, so it frees nothing and writes nothing rather
            # than unloading a second time. Same for a switch that happened while we queued.
            snap = self.snapshot()
            want = {n: max(0, (snap.get(f"{n}_mib") or 0) - y.idle_vram_mib)
                    for n, y in cands.items()}
            if not any(want.values()):
                return {"declined": None, "by_actor": False, "would": [], "released": [],
                        "waiting": [], "freed_mib": 0}
            released, waiting = self._release_cands(cands, want)
            if released:
                self.lock.bump()          # seats cache who owns the card: drop that answer
            return {"declined": None, "by_actor": False, "released": released, "waiting": waiting,
                    "freed_mib": sum(r["freed_mib"] for r in released)}

    def _release_cands(self, cands: dict, want: dict) -> tuple[list, list]:
        """Ask each workload that owes memory to let go of it, through the normal release path, and
        say which didn't. Called with the GPU lock held; `want` is what the card said each one held
        above its idle weight, read inside that lock.

        A working workload is skipped rather than interrupted - work in flight is never broken into,
        whatever asked for the card - and a workload whose service is down is reported: there is
        nothing to ask. One that answers "unloaded" while the card still shows its weights is
        reported `stuck`: the release path's confirm can be more generous than `idle_vram_mib`, or a
        node may not give the memory back, and it is the card that decides, not the server's reply.
        Whoever is watching retries both kinds, bounded for the stuck one, because a workload that
        never does unload would otherwise be asked for as long as the reason lasted.
        """
        released, waiting = [], []
        for n, y in cands.items():
            if not want[n]:
                continue
            if y.service_active() and (busy := y.busy()):
                waiting.append({"workload": n, "why": busy})
                continue
            if not y.service_active():
                waiting.append({"workload": n,
                                "why": f"a {y.label} process holds {want[n]} MiB but "
                                      f"{y.service.unit or 'its service'} is stopped"})
                continue
            try:
                # wait=0: anything still working was skipped above, and the release path's
                # own timeout keeps this inside one poll rather than a whole handover's.
                y.release(0, self.gate_release_timeout_s)
            except (Busy, ReleaseFailed, SwitchError) as e:
                waiting.append({"workload": n, "why": str(e) or type(e).__name__})
                continue
            left = max(0, (self.snapshot().get(f"{n}_mib") or 0) - y.idle_vram_mib)
            if left:
                # It answered "unloaded" and the card still shows its weights. Not done, and not
                # recorded as released: the row says "unloaded" and must only name what the card
                # agrees is clear. `stuck` is what the watcher counts its bounded retries on.
                waiting.append({"workload": n, "stuck": True, "held_mib": left,
                                "why": f"says it unloaded and still holds {left} MiB"})
                continue
            released.append({"workload": n, "freed_mib": want[n]})
        return released, waiting

    # --- pressure: an app vhotseat doesn't manage takes the card ---------------------

    PRESSURE_LOCK = "pressure release"     # the lock's label, and how a sister actor is spotted

    def pressure_readings(self, claimed=frozenset(), card: tuple | None = None) -> dict:
        """What the card, the samples and the config say about pressure, in one place so the trigger
        and the reports can't drift apart. It reads: it never takes the lock, releases nothing and
        loads nothing, which is what lets `vhotseat why` answer "was that release mine?".

        `card` is a card already read - the (sums, every holder) pair `probe` and the watcher keep
        for their own use. A report that read the card a second time to explain a decision would
        both double the cost of every status probe and be able to disagree with the numbers the
        decision was made on.

        Every answer carries the numbers, and when this is not pressure, `why` names which of the
        three conditions is missing - a card out of working room, out of it for long enough, and out
        of it because of memory vhotseat can't manage rather than memory it can ask back.

        `claimed` are the pids an open claim already covers: a game is phase 1's, already given the
        card by its rule, and counting its memory again would write a second episode over the same
        gigabytes.
        """
        cfg = self.pressure_cfg
        out = {"enabled": cfg["enabled"], "min_free_mib": cfg["min_free_mib"], "grow_mib": cfg["grow_mib"],
               "sustain_s": cfg["sustain_s"], "headroom_mib": cfg["headroom_mib"],
               "end_margin_mib": cfg["end_margin_mib"], "cooldown_s": cfg["cooldown_s"],
               "ignore": list(cfg["ignore"])}
        if not cfg["enabled"]:
            return {**out, "why": "pressure.enabled is false: nothing is released for an app "
                                  "vhotseat doesn't manage"}
        snap, rows = card if card else self.snapshot_all(limit=0)
        if snap.get("free_mib") is None:
            return {**out, "why": f"the card couldn't be read ({snap.get('error')})"}
        out.update({"free_mib": snap["free_mib"], "total_mib": snap.get("total_mib"),
                    "other_mib": snap.get("other_mib") or 0})
        claimed = set(claimed)
        ignore = self.pressure_ignores()
        growers = pressure.growers(rows, ignore, claimed)
        if growers:
            grower = dict(growers[0])
            shared = getattr(self.gpu, "shared", lambda: {})()
            grower["shared_mib"] = (shared or {}).get(grower["pid"]) or 0
            out["grower"] = grower
        floor = self.telemetry.floor_mib() if self.telemetry else None
        grew = pressure.grow_mib(rows, floor, ignore, claimed)
        out.update({"floor_mib": floor, "grew_mib": grew})
        if floor is None:
            # No samples is not a card with nothing else on it. Without a recorded baseline there is
            # no such thing as growth, and guessing one would unload models for a card that has been
            # like this all along - so the sampler (`watch`, `web`, `record`) is part of the trigger.
            return {**out, "why": "no samples to measure the growth against: run `vhotseat watch` "
                                  "or `vhotseat web`"}
        if grew < cfg["grow_mib"]:
            return {**out, "why": f"the memory vhotseat doesn't manage is only {grew} MiB over its "
                                  f"usual {floor} MiB floor, needs {cfg['grow_mib']} MiB"}
        low = (self.telemetry.below_free(cfg["min_free_mib"], max(2 * cfg["sustain_s"], 120))
               if self.telemetry else None)
        if not low:
            return {**out, "why": f"the card hasn't been under {cfg['min_free_mib']} MiB free for "
                                  f"long enough to tell"}
        low_for_s = max(0.0, time.time() - low[0])
        out.update({"low_for_s": int(low_for_s), "samples": low[1]})
        if low_for_s + 1 < cfg["sustain_s"]:
            return {**out, "why": f"the card has been under {cfg['min_free_mib']} MiB free for "
                                  f"{int(low_for_s)} s, needs {cfg['sustain_s']} s"}
        if snap["free_mib"] >= cfg["min_free_mib"]:
            # The samples say it was short often enough; this read says it isn't now. Freeing a model
            # for room that has already come back is the whole cost with none of the benefit.
            return {**out, "why": f"the card is back above {cfg['min_free_mib']} MiB free"}
        need = pressure.need_mib(snap["free_mib"], cfg["min_free_mib"], cfg["headroom_mib"])
        candidates = self.pressure_candidates(snap)
        targets = pressure.targets(candidates, need)
        out.update({"need_mib": need, "candidates": [n for n, _ in candidates], "targets": targets})
        if not targets:
            return {**out, "why": "nothing of ours on the card will give way for pressure"}
        return out

    def pressure_ignores(self) -> frozenset:
        """The `pressure.ignore:` set, built from the config block as it is now rather than once at
        startup: the block is a plain dict, and a policy that can't be changed while the process
        runs is a policy a later dashboard can't edit."""
        return pressure.ignores(self.pressure_cfg["ignore"])

    def pressure_candidates(self, snap: dict | None = None) -> list[tuple[str, int]]:
        """The workloads that could pay for the room, lowest priority first, each with the MiB it
        would give up: only ones whose `yields_to:` covers `pressure`, and only ones holding more
        than their idle weight, because the app that grew is short of VRAM and not of the RAM a model
        with no card weights costs.

        A busy one stays on the list. It is left alone when the release runs and asked again when it
        goes idle - work in flight is never interrupted, whatever asked for the card - and dropping it
        here would mean an episode quietly freeing less than it decided to.
        """
        snap = snap if snap is not None else self.snapshot_all()[0]
        out = []
        for n, y in sorted(self.workloads.items(), key=lambda kv: (kv[1].priority, kv[0])):
            held = (snap.get(f"{n}_mib") or 0) - y.idle_vram_mib
            if held > 0 and policy.yields(y.yields_to, policy.PRESSURE):
                out.append((n, held))
        return out

    def pressure_can_start(self) -> str | None:
        """Why a new episode can't start now, or None that it can. Both halves are read from the
        event log rather than remembered, because `watch` and the dashboard both run this check and
        neither may have a private idea of what the other has begun: one episode at a time, and no
        second one inside `cooldown_s` of the last row of the last one. An episode that stays open -
        because the app keeps its memory and the room never quite comes back - is not a free pass for
        a second round of releases either.
        """
        if self.events.episode_open() is not None:
            return "a pressure episode is already open"
        last = self.events.last_episode()
        when = row_time(last) if last else None
        if when is not None:
            age = (datetime.datetime.now().astimezone() - when).total_seconds()
            if age < self.pressure_cfg["cooldown_s"]:
                return (f"the last pressure episode was {int(age)} s ago, "
                        f"cooldown is {self.pressure_cfg['cooldown_s']} s")
        return None

    def pressure_start(self, claimed=frozenset(), source: str = "watch") -> dict:
        """Open a pressure episode if the readings say pressure, and release what it is owed.

        The readings are taken twice: outside the lock, so a box that isn't short pays one card read
        per poll and no lock at all, and again inside it. The second reading is what makes two
        pollers safe - whichever takes the lock first writes the episode and frees the card, and the
        other finds nothing left holding, writes nothing, and declines as `by_actor`.

        The episode row is written before anything is released, and it carries the workloads that
        were decided would pay for the room. Later polls act on that record and not on a fresh look
        at the card: a model loaded during an episode is the owner's, and a trigger that re-picked
        its targets every poll would take it back within seconds. Phase 1 learned that once.
        """
        blocked = self.pressure_can_start()
        if blocked:
            return _declined(blocked)
        readings = self.pressure_readings(claimed)
        if readings.get("why"):
            return _declined(readings["why"])
        grower = readings.get("grower") or {}
        name = grower.get("name") or "an unmanaged process"
        got = self.lock.try_take(f"{self.PRESSURE_LOCK} {name}")
        if not isinstance(got, Held):
            return _declined(f"{got or 'a switch'} in progress",
                             by_actor=bool(got and got.startswith(self.PRESSURE_LOCK)))
        with got:
            readings = self.pressure_readings(claimed)      # the card as whoever held the lock left it
            if readings.get("why"):
                return _declined(readings["why"])
            grower = readings.get("grower") or {}
            self.events.record("pressure", source=source, name=grower.get("name") or name,
                               pid=grower.get("pid"), other_mib=readings["other_mib"],
                               floor_mib=readings["floor_mib"], grew_mib=readings["grew_mib"],
                               free_mib=readings["free_mib"], total_mib=readings["total_mib"],
                               min_free_mib=readings["min_free_mib"], low_for_s=readings["low_for_s"],
                               samples=readings.get("samples"), need_mib=readings["need_mib"],
                               shared_mib=grower.get("shared_mib") or 0,
                               targets=readings["targets"], candidates=readings["candidates"])
            ep = self.events.episode_open() or {"episode": {}, "targets": readings["targets"]}
            out = self._pressure_release(ep, set(readings["targets"]), source)
            out["episode"] = self.events.episode_open()
            return out

    def pressure_release(self, ep: dict, source: str = "watch") -> dict:
        """Keep an open episode's release going: the workloads it was owed when it started, less the
        ones the card agrees came free, and nobody else. A target that was working is asked again on
        every poll while the episode lasts; one that says it unloaded and keeps its VRAM is asked a
        bounded number of times and then left, because that is an unload that doesn't work rather
        than a busy moment.

        Nothing is added to the list whatever the card now shows. If it goes shorter for a new
        reason, that is a new episode, and the cooldown decides when it may start.
        """
        owed = set(ep.get("targets") or []) - set(ep.get("released") or [])
        owed &= set(self.workloads)
        if not owed:
            return {"declined": None, "by_actor": False, "would": [], "released": [],
                    "waiting": [], "freed_mib": 0}
        snap = self.snapshot()
        if not any((snap.get(f"{n}_mib") or 0) > self.workloads[n].idle_vram_mib for n in owed):
            # Nothing it is still owed is on the card, so no lock and no card read is taken for as
            # long as the episode lasts afterwards.
            return {"declined": None, "by_actor": False, "would": [], "released": [],
                    "waiting": [], "freed_mib": 0}
        name = (ep.get("episode") or {}).get("name") or "an unmanaged process"
        got = self.lock.try_take(f"{self.PRESSURE_LOCK} {name}")
        if not isinstance(got, Held):
            return {"declined": f"{got or 'a switch'} in progress",
                    "by_actor": bool(got and got.startswith(self.PRESSURE_LOCK)),
                    "would": sorted(owed), "released": [], "waiting": [], "freed_mib": 0}
        with got:
            return self._pressure_release(ep, owed, source)

    def _pressure_release(self, ep: dict, targets: set, source: str) -> dict:
        """The release half, with the GPU lock held: re-read the card inside it, ask the workloads
        the episode was owed, and write the rows. The re-read is what makes the second arriving
        poller write nothing at all - inside the lock the card is already what the first one left it,
        so there is one `pressure-release` row and one unload per workload.
        """
        snap = self.snapshot()
        cands = {n: y for n, y in self.workloads.items()
                 if n in targets and policy.yields(y.yields_to, policy.PRESSURE)}
        want = {n: max(0, (snap.get(f"{n}_mib") or 0) - y.idle_vram_mib) for n, y in cands.items()}
        if not any(want.values()):
            return {"declined": None, "by_actor": False, "would": sorted(cands), "released": [],
                    "waiting": [], "freed_mib": 0}
        released, waiting = self._release_cands(cands, want)
        name = (ep.get("episode") or {}).get("name")
        if released:
            self.events.record("pressure-release", source=source, name=name,
                               workloads=[r["workload"] for r in released],
                               freed=[{"workload": r["workload"], "mib": r["freed_mib"]}
                                      for r in released],
                               freed_mib=sum(r["freed_mib"] for r in released))
            self.lock.bump()              # seats cache who owns the card: drop that answer
        if waiting:
            known = {w["workload"]: w for w in (self.events.episode_open() or {}).get("waiting", [])}
            for w in waiting:
                # How many tries the log has already seen for this one, which is what the watcher
                # counts its bounded retries on. Read back rather than remembered, so both pollers
                # are counting the same three.
                w["attempts"] = (known.get(w["workload"], {}).get("attempts") or 0) + 1
                self.events.record("pressure-release-waiting", source=source, name=name, **w)
        return {"declined": None, "by_actor": False, "would": sorted(cands), "released": released,
                "waiting": waiting, "freed_mib": sum(r["freed_mib"] for r in released)}

    def pressure_over(self, ep: dict) -> str | None:
        """Why an open episode is over, or None that it isn't: the room is back above the wider
        margin, or the process that grew has gone.

        The wider margin is the point. Ending an episode at the same line that started it would
        restart it the moment the grower allocated again - unload and reload ~20 GB over and over -
        and on a card with under a gigabyte of slack there is no room in between to be quiet in.
        """
        cfg = self.pressure_cfg
        free = self.snapshot().get("free_mib")
        if free is not None and free >= cfg["min_free_mib"] + cfg["end_margin_mib"]:
            return f"the card is back to {free} MiB free"
        name = (ep.get("episode") or {}).get("name")
        if name and not any((p.get("name") or "").lower() == name.lower()
                            for p in host.processes().values()):
            return f"{name} is gone"
        return None

    def pressure_status(self, claimed=frozenset(), card: tuple | None = None) -> dict:
        """What to show about pressure: the numbers the trigger is reading now, the episode if one
        is open, and the most recent episode whether or not it is.

        The open and the last one come from the event log, so an episode is explained the same way
        in the process that started it, in the other process watching it, and in a `vhotseat status`
        run an hour after it ended. `would` is the answer to "would it fire right now", which is the
        question an owner asks when nothing moved.
        """
        r = self.pressure_readings(claimed, card=card)
        out = {"enabled": r["enabled"], "min_free_mib": r["min_free_mib"], "grow_mib": r["grow_mib"],
               "sustain_s": r["sustain_s"], "headroom_mib": r["headroom_mib"],
               "end_margin_mib": r["end_margin_mib"], "cooldown_s": r["cooldown_s"],
               "ignore": list(r["ignore"]), "would": not r.get("why"), "why": r.get("why"),
               "open": self.events.episode_open(), "last": self.events.last_episode()}
        for key in ("free_mib", "total_mib", "other_mib", "floor_mib", "grew_mib", "grower",
                    "low_for_s", "samples", "need_mib", "candidates", "targets"):
            if key in r:
                out[key] = r[key]
        return out

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
        decided. So has a workload that won't give way for the newcomer (`yields_to:`):
        the request is refused, the typed switch goes through and logs the override.
        A request that changes nothing (the workload already holds the card, so nothing
        new would load) is not refused either: a batch mid-flight carries on, which is
        the same promise the guard makes to a render in flight."""
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
                    done = self.acquire(name, left, load=load, requested=request)
                except PolicyRefused as e:
                    self._policy_refused("switch", name, e.workloads, str(e))
                    self.events.record("switch", to=name, ok=False, error=str(e))
                    util.log(f"switch to {name} refused: {e}")
                    return {"ok": False, "error": str(e), "exception": e}
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
