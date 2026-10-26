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

import contextlib
import datetime
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import claims, host, notify, policy, pressure, util
from . import config as cfgmod
from .config_edit import version_of
from .drivers.base import Workload
from .errors import Busy, PolicyRefused, ReleaseFailed, SwitchError
from .events import EventLog, row_time
from .lock import GpuLock, Held, Locked, write_turn
from .telemetry import READ_ERRORS

NONE = "none"


def _declined(why: str, by_actor: bool = False) -> dict:
    """A pressure call that did nothing answers in the same shape as one that did: whoever asked
    can read `released` without knowing which of four ways out was taken."""
    return {"declined": why, "by_actor": by_actor, "episode": None, "released": [],
            "waiting": [], "freed_mib": 0, "would": []}


def _stamp(path: Path) -> tuple:
    """What says the file is a different file: its length and its mtime to the nanosecond, so a
    save inside the same second as the last look is still a change."""
    st = path.stat()
    return st.st_mtime_ns, st.st_size
# Set in a hold's command's environment, so a nested switch to the held workload
# is a no-op instead of waiting on the lock the hold owns.
HOLD_ENV = "VHOTSEAT_HOLD"


class Arbiter:
    def __init__(self, workloads: list[Workload], gpu, lock: GpuLock, events: EventLog, *,
                 gate_release_timeout_s: float = 20, release_timeout_s: float = 60,
                 machine_ram=host.machine_ram, telemetry=None, power: dict | None = None,
                 claim_rules: list | None = None, claim_ignore=(), claim_poll_s: float = 2.0,
                 pressure_cfg: dict | None = None, ask_cfg: dict | None = None,
                 config_path=None, notifier=None):
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
        # What an unanswered ask is worth: the `ask:` block with the defaults in it. They go into
        # the `ask` row when it opens, so a question is waited on for the length it was asked
        # with, whatever the file says by then, and the fallback is what closes it when nobody
        # answers (see `expire_asks`).
        self.ask = policy.ask_cfg(ask_cfg)
        # How a question reaches a person. The ask itself is a row in the log; this only carries
        # it (see notify.py), and a box that shows nothing still has the question on its
        # dashboard and in `status`, which is what `notifications:` says.
        self.notify = notifier if notifier is not None else notify.build()
        # How often a process waiting on an answer looks at the log. Not a config key: it costs a
        # small file read, and what it is waiting for is a person.
        self.ask_poll_s = 1.0
        # How many times a waiter tries to write an expired ask's fallback before it stops waiting
        # on that question: at the defaults, a 1 s poll and a 5 s wait for the log's turn, five
        # attempts is about half a minute of trying, and a request hung forever on a wedged log is
        # worse than one refused as unanswered - which is how the caller reports it, and the
        # question stays open in the log for whoever can write it. An attribute beside
        # `ask_poll_s` and not a config key: no answer to a question is waiting on it.
        self.ask_write_tries = 5
        # When each workload was last seen working, in a file every vhotseat process on this box
        # reads and stamps: the question has to be able to say "used 40 s ago" rather than
        # "loaded", because "used 40 s ago" is what tells the owner to pause their work first.
        self.used_path = events.path.parent / "last-used.json"
        # Where the config this was built from lives. A gate, a guard's `--request` and
        # `Seat.acquire` read the config in the process that asks, so a save reaches them by
        # itself; a `web` or a `watch` has been running since before it, and reloads from this
        # path. None for an arbiter built from a dict by a library caller or a test: nothing to
        # watch, and nothing to save back.
        self.config_path = Path(config_path) if config_path else None
        self._config_stamp: tuple | None = None
        # The version of the text whose rules are in force: "seen" means installed, not looked at, so
        # a reading that never went in can't mark newer bytes as handled.
        self._adopted: str | None = None
        # One thread at a time reads, parses and installs a config. A `web` is the claim watcher plus
        # a request thread per page tab, and `watch` re-reads too: without this, the reload that read
        # the file before someone else's save and stopped part way through its parse can come back
        # afterwards and put what it was reading over the rules that save installed.
        self._policy_lock = threading.RLock()
        self.config_error: str | None = None

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

    def shared_total_mib(self) -> int | None:
        """Total shared GPU memory across every process with a context on the card: system RAM the
        card borrowed once it ran out, which is Task Manager's "shared GPU memory" column.

        Evidence, never a decision - see pressure.py's docstring. Measured on this box: 52206 MiB of
        it, 50.9 GiB of that Strata, on a card that was merely full. A backend that has no such
        figure (Linux NVML) answers None rather than 0, because 0 would read as "nothing spilled".
        """
        fn = getattr(self.gpu, "shared", None)
        if not fn:
            return None
        try:
            per = fn()
        except Exception:
            return None
        # {} is the backend saying "I have no such counter" (Linux NVML), not "nothing spilled":
        # None keeps that difference instead of inventing a zero.
        return sum(per.values()) if per else None

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

    def refuse_for(self, reason: str, gpu: dict) -> list[str]:
        """The loaded workloads that refuse `reason` outright: their `yields_to:` doesn't cover it
        and their `asks_for:` doesn't ask about it. A refusal is a decision the config already
        made, which is why it is answered apart from the workloads that merely want being asked."""
        return [n for n in self.loaded(gpu)
                if n != reason and policy.choice(self.workloads[n].yields_to,
                                                 self.workloads[n].asks_for, reason) == "keep"]

    def ask_for(self, reason: str, gpu: dict) -> list[str]:
        """The loaded workloads whose answer for `reason` is the question: `asks_for:` names it.
        What waiting costs depends on who is asking - a workload's own request waits for the
        answer, a claim and an episode run meanwhile and are owed the release as soon as it comes
        - but every path asks through `ask_now`, so there is one open ask per pair, in the log,
        for whichever process gets here first."""
        return [n for n in self.loaded(gpu)
                if n != reason and policy.may_ask(self.workloads[n].yields_to,
                                                  self.workloads[n].asks_for, reason)]

    def claim_refusal(self, hits: list | None = None) -> str | None:
        """The sentence that refuses a load nobody chose while a claimed process owns the card, or
        None when nothing is claimed. One sentence because the claim says the same thing whoever is
        asked - a gate hook, a guard's `--request`, a proxy's Seat - and it is asked more than once
        per request on purpose: a request that waited for an Ask me answer has just been out of the
        decision for the length of `ask.timeout_s`, and the game that started inside that is exactly
        what the rule is about. The gate asks under the lock, as it always has; a request that
        waited outside it asks again before the handover it was waiting for begins.

        `hits` reuses a scan the caller has already done - the process table is read tens of
        milliseconds at a time, but not twice for no reason inside one locked decision."""
        hits = self.claim_hits() if hits is None else hits
        return None if not hits else (
            f"the card is claimed by {claims.describe(hits)}: nothing new loads while it runs")

    def wont_give_way(self, reason: str, gpu: dict) -> list[str]:
        """The loaded workloads that won't make room for `reason` on the config's own authority:
        the ones that refuse it, and the ones that want asking first.

        A release gets back the ones the owner has already answered "unload" for (`consent`), so
        this is the list of what still needs refusing or asking, never the list of what may be
        unloaded."""
        return [n for n in self.loaded(gpu)
                if n != reason and not policy.yields(self.workloads[n].yields_to, reason,
                                                     self.workloads[n].asks_for)]

    def policy_why(self, requester: str, blockers: list[str], gpu: dict) -> str:
        """The refusal in words a person can act on: who is in the way, how much it is
        holding, and what it does give way for - which is the key to change."""
        who = self.label(requester)
        bits = [f"{self.label(n)} holds {gpu.get(f'{n}_mib')} MiB and "
                f"{policy.why_not(self.workloads[n].yields_to, self.workloads[n].asks_for, requester, who)}"
                for n in blockers]
        return f"nothing will make room for {who}: " + "; ".join(bits)

    def kept_for_policy(self, reason: str, gpu: dict | None = None) -> list[dict]:
        """The loaded workloads that won't give way for a release (`claims`, `pressure`) because
        they *refuse* it. A release is never refused, so these keep holding: whoever asked for
        the card may now be short of exactly that much, which is why each one is worth a row in
        the log and a line on the dashboard rather than silence.

        A workload that wants being asked isn't here: it hasn't refused anything, it is owed to
        the release the way any target is, and it gets its `policy-kept` row when the answer that
        comes back says keep (`ask_before_release`). Saying "it kept its model because it
        refuses" about a workload whose owner hasn't answered yet is the lie this separates."""
        gpu = self.snapshot() if gpu is None else gpu
        return [{"workload": n, "held_mib": gpu.get(f"{n}_mib") or 0}
                for n in self.refuse_for(reason, gpu)]

    def kept_why(self, reason: str, name: str, held_mib: int, answered: dict | None = None) -> str:
        """Why something stayed loaded, in the sentence the row and the banner show: what the
        config says when nothing was ever asked, and whose answer it was when an ask closed it."""
        w = self.workloads[name]
        # Named the way the newcomer is named everywhere else: a workload by its label, a claim and
        # an episode by the phrase, so the same refusal doesn't read one way in a row and another
        # on the page.
        who = policy.PHRASE.get(reason) or self.label(reason)
        mid = (policy.answered_why(who, answered["answer"], answered.get("by") or "owner")
               if answered else policy.why_not(w.yields_to, w.asks_for, reason, who))
        return (f"{self.label(name)} holds {held_mib} MiB and {mid}: it keeps its model, and "
                "whatever asked for the card may be short of that")

    def _policy_refused(self, source: str, requester: str, blockers: list[str], why: str) -> None:
        """One row for one refusal, so the dashboard can warn about it and `why` and
        `/api/events` can explain it after the fact. `ok: false` is what marks it red on
        the charts, where the load it stopped is the point."""
        self.events.record("policy-refused", source=source, requested=requester, ok=False,
                           workloads=blockers, why=why)

    # --- when a workload was last working -------------------------------------------

    def last_used(self) -> dict:
        """{workload: when it was last seen working}, read from the state file.

        A file and not process memory because `web` asks the question and `watch` may be the one
        that saw the workload working, and the other way round: on this box both may be running,
        and a gate in a fresh process asks about a card neither of them is watching. Empty means
        nothing has recorded it, which is said as unknown - never as idle."""
        try:
            raw = json.loads(self.used_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {n: v for n, v in raw.items() if isinstance(v, (int, float))} if isinstance(raw, dict) else {}

    def note_used(self, name: str, when: float | None = None) -> None:
        """Record that `name` was seen working at `when` (now, by default).

        The merge is a max per workload, and the read of the file, the merge and the write back are
        one turn of it, taken the way a config save and a log row are taken: `watch` and the
        dashboard both stamp the same file from different processes, and each stamp is a
        read-check-write over the whole of it. Unguarded, an older reading read before the other
        process wrote gets written back afterwards and puts a stale stamp back - which is the small
        harm - and the two writers also arrive at one shared temp name, where the loser's rename
        fails outright. Inside the turn, whoever writes last has read the other's newest stamp and
        keeps it: an older one read off the file is dropped rather than put back, and a stamp that
        was genuinely the newer one is never lost.

        A workload that is genuinely not being used says "idle for ..." truthfully, and the answer a
        question gets doesn't read the stamp at all: it is there to tell the owner to pause their
        work, not to decide for them."""
        if name not in self.workloads:
            return
        when = time.time() if when is None else when
        try:
            # The directory before the turn: the turn's lock file lives beside the stamp file, and
            # on a box that has never stamped anything neither exists yet.
            self.used_path.parent.mkdir(parents=True, exist_ok=True)
            with write_turn(self.used_path, what="stamping what was working"):
                merged = self.last_used()
                if (merged.get(name) or 0) >= when:
                    return               # someone else saw it working later than this
                merged[name] = when
                fd, tmp = tempfile.mkstemp(dir=str(self.used_path.parent),
                                           prefix=self.used_path.name + ".", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(json.dumps(merged, sort_keys=True))
                    util.replace(Path(tmp), self.used_path)
                finally:
                    with contextlib.suppress(OSError):
                        Path(tmp).unlink(missing_ok=True)
        except (OSError, Locked):
            # A state file that can't be written, or a writer that won't let go, costs the question
            # its "used 40 s ago" and nothing else: it is asked with "nothing has recorded it"
            # instead, which is true of this file rather than a guess about the workload.
            return

    def used_ago(self, name: str) -> float | None:
        """How long ago a workload was last seen working, or None when nothing recorded it."""
        when = self.last_used().get(name)
        return None if when is None else max(0.0, time.time() - when)

    def used_report(self) -> dict:
        """`last_used` in `/api/status`: per workload, the stamp, how long ago it is, and the words
        the question uses ("used 40 s ago", "idle for 25 min"). No stamp is said as no stamp."""
        stamps = self.last_used()
        out = {}
        for n in self.workloads:
            when = stamps.get(n)
            ago = None if when is None else max(0.0, time.time() - when)
            out[n] = {"epoch": when, "ago_s": ago, "phrase": policy.used_phrase(ago)}
        return out

    # --- the ask: a question in the log, an answer from the owner ---------------------

    # The lock's label while an ask is being opened, and how a sister actor is spotted: the same
    # single-actor discipline as a pressure episode, because `watch`, the dashboard and a gate in
    # a fresh process can all decide to ask about the same pair in the same moment.
    ASK_LOCK = "ask"

    def ask_view(self, ask: dict) -> dict:
        """An `ask` row plus what a person reading it needs around it: the labels, how long ago
        the workload was last working *when it was asked*, how much of the wait is left, and the
        sentences. The dashboard, the notification and `status` all render this, so the question
        can't be worded one way in a toast and another on the page."""
        view = dict(ask)
        view["label"] = self.label(view.get("workload"))
        requester = view.get("requester")
        view["requester_label"] = (policy.PHRASE.get(requester, requester or "")
                                   if requester in (policy.CLAIMS, policy.PRESSURE)
                                   else self.label(requester))
        when, used = row_time(view), view.get("last_used")
        view["ago_s"] = None if (used is None or when is None) else max(0.0, when.timestamp() - used)
        view["age_s"] = None if when is None else max(0.0, time.time() - when.timestamp())
        view["left_s"] = max(0.0, (view.get("timeout_s") or 0) - (view["age_s"] or 0))
        if view.get("answer"):
            view["answer_says"] = (f"{view['label']} unloads" if view["answer"] == policy.RELEASE
                                   else f"{view['label']} stays loaded")
        view["text"] = notify.ask_text(view)
        return view

    def open_asks(self) -> list[dict]:
        """The questions the log shows as unanswered, in the shape `/api/status` shows them and the
        buttons answer: same rows for every process on the box, whoever opened them."""
        return [self.ask_view(a) for a in self.events.open_asks()]

    def asks_for(self, reason: str, scope: str = "") -> dict[str, dict]:
        """The asks this reason and this scope have, keyed by workload, newest last.

        Scope is why an answer is not standing permission: "unload Strata" is consent for the game
        that was playing when it was said, and the same rule matching a different game process - or
        the card going short again after an episode ended - is a different question with a different
        answer. The scope is in the row, so this is an equality, not a reading of timestamps: a row
        stamped to the second cannot tell a game that closed and restarted inside that second apart,
        and two games playing at once are two claims of the same rule.
        """
        out: dict[str, dict] = {}
        for a in self.events.asks():
            if a.get("requester") == reason and (a.get("scope") or "") == scope and a.get("workload"):
                out[a["workload"]] = a
        return out

    def ask_now(self, workload: str, requester: str, *, held_mib: int, source: str,
                name: str | None = None, note: str | None = None, free_mib: int | None = None,
                pid: int | None = None, held: bool = False, scope: str = "") -> dict | None:
        """Open the one open ask for this (workload, requester, scope), or return the one that is
        already open. Returns the row, or None when this process couldn't open one.

        The scope is whose question it is, and it is part of the identity rather than a detail to
        check afterwards: a claim's questions belong to the game process that claimed the card, an
        episode's to that episode, and a load nobody chose has no scope beyond the requester it is
        named for. Two games on one workload therefore get two questions with two countdowns and two
        buttons, each release acts on its own answer, and a question is closed only when its own
        scope ends - never because somebody else's turned up.

        Under the GPU lock and with the log re-read after taking it, exactly as a pressure episode
        is opened: two actors can be told "nothing is open" in the same moment before either has
        written a row, and the one that arrives second at the lock has to find the first one's
        question rather than ask the owner the same thing twice - a game and a queue asking about
        one workload would otherwise produce two toasts and two answers to reconcile. A caller
        that already holds the lock passes `held`.

        The row carries the numbers the wait and the sentence are built from - the timeout and
        fallback as of asking, what the workload holds, when it was last seen working - because a
        question has to keep meaning the same thing to whoever reads it later.

        The row is written under the lock; the notification is raised after it is given back, when
        this process took the lock itself. Showing a toast is a call into a GUI stack that nobody
        controls - a box with no desktop session for this process can sit in it for seconds - and a
        handover must not wait on that, so the writing of the question and the shouting of it are
        deliberately separate. A caller already inside a release (`held=True`) has the lock for the
        release itself, and the question goes out from inside it because that is where the numbers
        were read.
        """
        existing = self.events.open_ask(workload, requester, scope)
        if existing:
            return existing
        if not held:
            got = self.lock.try_take(f"{self.ASK_LOCK} {requester}")
            if not isinstance(got, Held):
                # Someone else is mid-open, mid-release or mid-switch. Their question - if they
                # wrote one - is the one to wait on; if they didn't, this caller decides as it
                # would have without the asking rather than waiting on nothing.
                return self.events.open_ask(workload, requester, scope)
            with got:
                row = self._open_ask(workload, requester, held_mib=held_mib, source=source,
                                     name=name, note=note, free_mib=free_mib, pid=pid, scope=scope)
            return (self._raise_question(row) if row
                    else self.events.open_ask(workload, requester, scope))
        row = self._open_ask(workload, requester, held_mib=held_mib, source=source, name=name,
                             note=note, free_mib=free_mib, pid=pid, scope=scope)
        return self._raise_question(row) if row else self.events.open_ask(workload, requester, scope)

    @staticmethod
    def claim_scope(pid: int | None) -> str:
        """Which questions are this claim's: the game process that took the card. The pid is the
        whole of it, because the claim that owned it has ended and been recorded before any other
        process can own the same rule - and two games playing at once are two pids asking about the
        same workload, which is the case a pid and nothing else separates."""
        return f"pid{pid}" if pid else ""

    @staticmethod
    def episode_scope(episode: dict) -> str:
        """Which questions are this episode's: the id of the row that opened it. An episode that has
        ended is not the next shortness of the same card, so its unanswered question must not be what
        the next episode is joined to - and `cooldown_s` can be shorter than `ask.timeout_s`, so the
        next one can arrive while that question is still counting down. The id rather than the row's
        timestamp because rows are stamped to the second, and a card that has been short all afternoon
        can open its next episode inside one."""
        return f"ep{episode['id']}" if episode.get("id") else ""

    def ended_says(self, claim: dict) -> str:
        """The sentence on a question retired because the claim itself ended."""
        return (f"{claim.get('name') or 'the game'} (process {claim.get('pid')}) exited: the "
                "question was asked about that game, and there is nothing left to free for it")

    def close_ask(self, ask_id: str, *, closed_by: str, reason: str) -> tuple[int, dict]:
        """Retire an open ask that has nothing left to be answered: 200 when it was open, 409 when
        it has an answer or was retired already, 404 when the log holds no such ask, 503 when the
        log wouldn't let go.

        Whether it was still open is decided in the log, in the same turn as the closing row, for
        the same reason an answer is decided there: the owner clicking Unload now and a watcher
        noticing the game has gone are one write apart, and only one of them should land. Nothing is
        unloaded either way: a close takes away a question, it never releases anything.
        """
        state, row = self.events.end_ask(ask_id, closed_by=closed_by, reason=reason)
        if state == "missing":
            return 404, {"error": f"no ask {ask_id} in the event log"}
        if state == "answered":
            return 409, {"error": f"ask {ask_id} was answered {row.get('answer')} already",
                         "ask": self.ask_view(row)}
        if state == "closed":
            return 409, {"error": f"ask {ask_id} was closed ({row.get('closed_by')}) already",
                         "ask": self.ask_view(row)}
        if state == "locked":
            return 503, {"error": f"the event log is busy: nothing was written for ask {ask_id}"}
        util.log(f"ask: closed an unanswered question: {reason}")
        return 200, {"ask": self.ask_view(row)}

    def end_scope_asks(self, reason: str, scope: str, *, closed_by: str, says: str) -> int:
        """Retire the questions one scope was asking, once that scope is over: a claim that ended,
        an episode that ended. Returns how many.

        A question is asked *about* a thing that wants memory freed, and it stops meaning anything
        when that thing is gone. Left open it is a card the owner is invited to act on for a game
        that has already exited, with a button no release will ever act on, and nothing waits on it:
        the only thing that would close it is the fallback, which would record a decision about an
        unload nobody wants any more. Only its own scope ending closes it, so the other game still
        playing keeps its question, and the next claim or episode - a different scope - is asked its
        own.
        """
        n = 0
        for ask in self.events.open_asks():
            if ask.get("requester") != reason or (ask.get("scope") or "") != scope:
                continue
            code, _ = self.close_ask(ask["id"], closed_by=closed_by, reason=says)
            if code == 200:
                n += 1
        return n

    def _open_ask(self, workload: str, requester: str, *, held_mib: int, source: str,
                  name: str | None, note: str | None, free_mib: int | None,
                  pid: int | None, scope: str = "") -> dict | None:
        """Write the `ask` row and return it, or None when the question is already in the log -
        the same recheck after taking a lock that a pressure episode does, and the reason a pair gets
        one question and one notification rather than two. Called with the GPU lock held.

        The look and the write are one turn of the log, for the same reason an answer is: two
        processes that both find no ask for the pair and both write one leave two open questions for
        one pair, which is two notifications and two countdowns for the same decision, and a second
        one to answer. The GPU lock usually keeps them apart, but a `web`, a `watch` and a gate in a
        fresh process reach here from three different places, and the log turn is what makes "is it
        already asked?" mean the same thing as the row that follows it.
        """
        with self.events.turn(what="opening an ask"):
            if self.events.open_ask(workload, requester, scope) is not None:
                return None              # written between the look above and this turn
            row = {"id": uuid.uuid4().hex[:12], "workload": workload, "requester": requester,
                   "source": source, "held_mib": held_mib, "free_mib": free_mib,
                   "timeout_s": self.ask["timeout_s"], "fallback": self.ask["fallback"],
                   "last_used": self.last_used().get(workload), "name": name, "note": note,
                   "scope": scope}
            if pid:
                row["pid"] = pid
            # The row's own time, chosen here rather than left to the log: the wait, the countdown
            # the question shows, and "when was it last working" are all measured from the moment
            # these numbers were taken, and the view built later has to be timed from the same
            # instant the row is stored with. `EventLog.record` takes a `time` it is given.
            row["time"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            self.events.record("ask", **row)
        return row

    def _raise_question(self, row: dict) -> dict:
        """Put the question in front of a person, once, and say in the log what it asked: the
        notification if this box can show one, the dashboard and `status` if not. The words are the
        same either way, and so is the answer path - the notifier's buttons call `answer_ask`."""
        view = self.ask_view(row)
        shown = self.notify.show_ask(view, lambda ask_id, answer: self.answer_ask(ask_id, answer))
        util.log(f"ask: {view['text']['headline']} - {view['text']['detail']}\n"
                 + (f"     answer within {policy.clock_phrase(view['left_s'])}" if shown
                    else "     no notification here: the question is on the dashboard")
                 + f" - {view['text']['no_answer']}")
        return row

    def answer_ask(self, ask_id: str, answer: str, by: str = "owner") -> tuple[int, dict]:
        """Answer an open ask: (200, the ask) when it was open, 409 when it has been answered or
        isn't open any more, 404 when the log has no such ask, 400 when the answer isn't one of
        the two it takes.

        The first answer wins, whoever wrote it: an owner clicking Keep a second before a watcher
        gives up and applies the fallback gets their own answer, and the fallback row is then
        simply ignored (`EventLog.asks`). That is also why there is no GPU lock here: an answer is
        a row, and taking the lock to write it would mean a question could not be answered while
        the handover it is waiting for is running - which is when the owner is most likely to be
        answering it.

        Whether this answer is the answer is decided inside the log, not in here: `EventLog.answer_ask`
        holds the log's write turn across the check that the ask is open and the row that answers it,
        so Keep written by the owner and Release applied by a timeout are one write apart rather than
        a race whose loser is told it won. 503 is the case where neither got written: nothing is
        recorded, and the question is still open to be answered again.
        """
        if answer not in policy.ANSWERS:
            return 400, {"error": f"an ask is answered {' or '.join(policy.ANSWERS)}, not {answer!r}"}
        if by not in policy.ANSWERED_BY:
            return 400, {"error": f"an ask is answered by {' or '.join(policy.ANSWERED_BY)}"}
        state, row = self.events.answer_ask(ask_id, answer, by)
        if state == "missing":
            return 404, {"error": f"no ask {ask_id} in the event log"}
        if state == "answered":
            return 409, {"error": (f"ask {ask_id} was answered {row.get('answer')} "
                                   f"({row.get('by') or 'owner'}) already"),
                         "ask": self.ask_view(row)}
        if state == "closed":
            return 409, {"error": f"ask {ask_id} was closed without an answer: "
                                  f"{row.get('closed_reason') or row.get('closed_by')}",
                         "ask": self.ask_view(row)}
        if state == "locked":
            return 503, {"error": f"the event log is busy: nothing was written for ask {ask_id} - "
                                  "the question is still open, answer it again"}
        if by == "owner":
            view = self.ask_view(row)
            util.log(f"ask: {self.label(row.get('workload'))} for "
                     f"{view['requester_label']}: you said {view['answer_says']}")
        return 200, {"ask": self.ask_view(row)}

    def expire_asks(self, reason: str | None = None) -> list[dict]:
        """Apply the `ask:` fallback to every open ask whose timeout has run out, and record it as
        `by: timeout`.

        Whoever notices is whoever writes it - the waiter, a watcher's next poll, the dashboard's
        status probe - because a question nobody answered has to close, and an open ask that never
        closes leaves a claim owed to a workload nobody is going to unload. The row it writes says
        it wasn't the owner who answered, so a fallback unload is never mistaken for a decision
        somebody made. Answers that arrived in the same moment win: `answer_ask` refuses the second
        row, and the log's first answer is the answer."""
        out = []
        for ask in self.events.open_asks():
            if reason and ask.get("requester") != reason:
                continue
            view = self.ask_view(ask)
            if view["left_s"] > 0:
                continue
            fallback = ask.get("fallback") or self.ask["fallback"]
            code, body = self.answer_ask(ask["id"], fallback, by="timeout")
            if code != 200:
                continue                      # someone answered it in the meantime: theirs counts
            util.log(f"ask: nobody answered about {self.label(ask['workload'])} in "
                     f"{policy.wait_phrase(ask.get('timeout_s') or 0)}: the ask: fallback says "
                     f"{body['ask']['answer_says']}")
            out.append(body["ask"])
        return out

    def await_asks(self, asks: list[dict], own_deadline: float | None = None) -> dict[str, str]:
        """Wait for the answers to these asks and return {workload: answer}.

        Polls the event log, which is what makes this need no new IPC: the dashboard's button, a
        toast's button in whichever process raised it, and another process applying the fallback
        all leave their answer in the same rows a gate in a fresh process then reads. It is the
        only way a proxy's `Seat.acquire` can wait for an answer, since nothing is listening on
        the proxy's side.

        When an ask's own timeout runs out, this applies and records its fallback, so the question
        closes once and says who didn't answer it. `own_deadline` - a gate hook's own time limit -
        stops *this* waiter without closing anything: the question stays open, still timed from
        when it was asked, for the next request and for the dashboard.

        Applying a fallback is writing a row, so it can fail: another writer may be holding the
        log's turn past the bounded wait, and then nothing has been written and the ask is as
        unanswered as it was. That is not an answer, and this used to read it as one - ignore the
        write, re-read the still-unanswered row, index a key that isn't there, and the waiting gate
        hook, a guard's `switch --request` or a proxy's `Seat.acquire` dies of `KeyError` in the
        middle of a handover. A fallback the log wouldn't take leaves the question pending: the
        next poll tries again against the same deadline, and an answer is never read out of a row
        that has none. Only a log that keeps refusing stops the wait at all, and then the request is
        refused as unanswered rather than hanging - and whoever can write that log, a watcher, the
        dashboard or the owner's next click, still closes the question.
        """
        pending = {a["id"] for a in asks if a.get("id")}
        out: dict[str, str] = {}
        refused: dict[str, int] = {}
        while pending:
            for ask_id in sorted(pending):
                cur = self.events.ask_by_id(ask_id)
                if cur is None:
                    pending.discard(ask_id)     # aged out of the retained log: nothing to wait on
                    continue
                if cur.get("closed_by") and not cur.get("answer"):
                    # Retired without an answer - the claim it was asked about has gone. No answer
                    # is coming, and waiting for one would hang the request on a moot question; an
                    # ask that was answered *then* retired still has its answer, and that is read.
                    pending.discard(ask_id)
                    continue
                if not cur.get("answer"):
                    if self.ask_view(cur)["left_s"] > 0:
                        continue
                    code, body = self.answer_ask(
                        ask_id, cur.get("fallback") or self.ask["fallback"], by="timeout")
                    if code == 503:
                        # The log wouldn't let go, so nothing was written: there is nothing here to
                        # decide on, and the question stays open exactly as it was.
                        refused[ask_id] = refused.get(ask_id, 0) + 1
                        if refused[ask_id] == 1:
                            util.log(f"ask: {body['error']}")
                        if refused[ask_id] >= self.ask_write_tries:
                            util.log("ask: the event log still won't let go, so this request stops "
                                     "waiting on the question rather than guessing at an answer")
                            pending.discard(ask_id)
                        continue
                    if code == 404:
                        pending.discard(ask_id)  # aged out between reading it and answering it
                        continue
                    cur = self.events.ask_by_id(ask_id) or cur
                    if not cur.get("answer"):
                        continue                # never decide from a row that has no answer in it
                out[cur["workload"]] = cur["answer"]
                pending.discard(ask_id)
            if not pending:
                break
            if own_deadline is not None and time.monotonic() >= own_deadline:
                break
            util.sleep(self.ask_poll_s)
        return out

    def ask_before_request(self, requester: str, *, own_deadline: float | None = None,
                           source: str = "request") -> tuple[set, list, str | None]:
        """The asking half of a load nobody chose - a gate, a guard's `--request`, a proxy's
        `Seat.acquire`. Returns (the workloads the answer freed, the workloads to refuse, the why).

        A workload request is the one requester that *can* wait: nothing is running that an unload
        would interrupt, and the newcomer is being refused or allowed either way. So instead of
        refusing a holder whose pair says Ask me, the request waits for the answer up to the ask's
        own timeout and then proceeds or refuses per the answer or the fallback.

        This runs before the GPU lock is taken, never under it: a handover that holds the lock for
        two minutes waiting for a person stops every other handover on the box, including the one
        a game's release is waiting for. Nothing is asked while another actor holds the lock -
        that path answers from the lock's note, as it always has, and that actor is deciding the
        same card right now.

        A `keep` is the existing refusal, with a why that says the owner chose it: the point is
        that the next refusal of the same kind reads as their decision rather than a rule they
        forgot they wrote.
        """
        gpu = self.snapshot()
        pairs = self.ask_for(requester, gpu)
        if not pairs:
            return set(), [], None
        self.expire_asks(requester)
        asks = [a for a in (self.ask_now(n, requester, held_mib=gpu.get(f"{n}_mib") or 0,
                                         source=source, name=self.label(requester),
                                         free_mib=gpu.get("free_mib")) for n in pairs) if a]
        if not asks:
            return set(), [], None      # couldn't open one: the normal path decides as it did
        answers = self.await_asks(asks, own_deadline=own_deadline)
        consent = {n for n, answer in answers.items() if answer == policy.RELEASE}
        by_id = {a["id"]: a for a in asks}
        for ask in asks:
            n = ask["workload"]
            answer = answers.get(n)
            if answer == policy.KEEP:
                cur = self.events.ask_by_id(ask["id"]) or by_id[ask["id"]]
                why = self.kept_why(requester, n, gpu.get(f"{n}_mib") or 0,
                                    {"answer": answer, "by": cur.get("by") or "owner"})
                return consent, [n], why
        if any(answers.get(a["workload"]) is None for a in asks):
            # This waiter ran out of time before the question did (a gate hook's own limit): the
            # ask stays open, and the load is refused for that reason rather than as a policy no
            # one ever answered.
            waiting = [a["workload"] for a in asks if answers.get(a["workload"]) is None]
            why = self.policy_why(requester, waiting, gpu)
            return consent, waiting, why
        return consent, [], None

    def ask_before_release(self, reason: str, cands: dict, want: dict, *, source: str,
                           name: str | None = None, note: str | None = None, since: str | None = None,
                           pid: int | None = None, scope: str = "",
                           snap: dict | None = None) -> dict:
        """The asking half of a release that is never refused - a claim, an episode: ask the
        candidates whose pair says Ask me, and return the ones that may be released now.

        Called with the GPU lock held, so the question is opened under it and re-checked after
        taking it (`ask_now`), and the answer a previous poll found is read back from the log
        rather than remembered: `watch` and the dashboard take turns at this, and the game runs
        meanwhile either way, so nothing is owed a faster answer than the next poll.

        A workload with no answer yet is not `waiting` and is not attempted: nothing is waiting to
        unload it, the game is simply short of that much memory, and spending the bounded "it says
        it unloaded and is still holding" retries on a workload nobody has tried to unload would
        hide the ones that are really stuck. An answer of keep gets its one `policy-kept` row, and
        the claim or episode goes on without it - which is the same shape as a refusal elsewhere,
        except that the row says the owner chose it.
        """
        asked = {n: y for n, y in cands.items() if policy.may_ask(y.yields_to, y.asks_for, reason)}
        if not asked:
            return cands
        self.expire_asks(reason)
        known = self.asks_for(reason, scope)
        said = self.events.kept_said(reason, name=name, note=note, since=since)
        for n in asked:
            ask = known.get(n)
            if ask is None:
                self.ask_now(n, reason, held_mib=want.get(n) or 0, source=source, name=name,
                             note=note, free_mib=(snap or {}).get("free_mib"), pid=pid, held=True,
                             scope=scope)
                continue
            answer = ask.get("answer")
            if answer == policy.RELEASE:
                continue                       # the owner's consent: it goes through the release
            if answer == policy.KEEP and n not in said:
                why = self.kept_why(reason, n, want.get(n) or 0,
                                    {"answer": answer, "by": ask.get("by") or "owner"})
                self.events.record("policy-kept", source=source, reason=reason, name=name,
                                   note=note, workload=n, held_mib=want.get(n) or 0, why=why)
                util.log(f"{why}")
        return {n: y for n, y in cands.items()
                if n not in asked or known.get(n, {}).get("answer") == policy.RELEASE}

    # --- picking up a saved policy -------------------------------------------------

    def reload_policy(self, force: bool = False) -> str | None:
        """Re-read the owner's config if the file has changed, and put the new rules in force.
        Returns why the file isn't a config now, or None that it is.

        Only the long-lived readers need this, and they all reach it the same way: `probe()` calls
        it, so `record`, the dashboard and its sampler see the file as it is now; `watch_pass` calls
        it before it acts on a claim or the pressure; and `Seat.acquire` (a proxy's gate, which is
        not a new process) calls it before it switches. Every other command - a gate, the ComfyUI
        guard's `--request`, a `switch` - is a new process that read the config as it started: for
        them this is one `stat` on a file they parsed themselves a moment ago.

        `web` and `watch` are where it matters: once the dashboard has saved a rule, the watcher on
        the same box is the one that has to obey it, and waiting for a restart would leave the owner
        looking at an editor that says one thing and a card that does another.

        The check is cheap and the swap is safe: the whole file is parsed and validated before
        anything is swapped, so a half-typed hand-edit changes nothing: the rules already running
        keep running, and the caller is told (`/api/status`'s `config_error`, and one log line the
        first time it notices, not one per poll). And the read, the parse and the swap are one turn
        of one lock, because a dashboard is a watcher thread and a request thread per page tab: a
        reload that read the file before a save can stop part way through its parse, and it must not
        be the one that installs rules last. What marks the file as seen is the reading whose rules
        actually went in, never a stamp picked up from bytes that didn't.

        What is re-read is the policy, the pressure numbers and the ask numbers. Which workloads
        there are, how to reach them, and what claims the card are what a restart is for: those
        build the objects and the probes rather than a number on them.
        """
        path = self.config_path
        if path is None:
            return None
        try:
            stamp = _stamp(path)
        except OSError as e:
            return self._config_error(f"{path}: {e.strerror or e}")
        if stamp == self._config_stamp and not force:
            return self.config_error
        # One turn of one lock for the whole read-parse-install: the watcher's pass, a dashboard's
        # request threads and the save itself all come through here, and a reading that started before
        # someone else's save must not be the last one to install what it read.
        with self._policy_lock:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # The file is there and it isn't text: an editor that saved it as Windows-1252 leaves
                # an en dash as one byte, and that byte is not a UTF-8 sequence. It's a config error
                # named the way `config_edit` names it when the dashboard tries to save over such a
                # file, not a reason for the reload to die in the middle of the process that is
                # looking after the card. Not recorded as seen either: a file caught mid-write by an
                # editor that doesn't write atomically looks like this too, and the next poll should
                # look again.
                return self._config_error(f"{path}: not utf-8 text")
            except OSError as e:
                # Not recorded as seen: a file we couldn't read isn't a file we've handled. A moment
                # when someone else's atomic write has the file half-present is a moment to try again
                # on the next poll, not a state to sit in until the mtime changes again.
                return self._config_error(f"{path}: {e.strerror or e}")
            if not force and version_of(text) == self._adopted:
                # The file moved - an editor that wrote it again without changing anything, a comment,
                # a `git pull` - and says what is already in force. Nothing to install, and this is
                # the reading that has been seen.
                self._config_stamp = stamp
                return self.config_error
            note = self.adopt_policy(text)       # takes the same lock; records `_adopted`
            if note is None:
                # Only a reading that actually put its rules in force marks the file as seen, and with
                # the stamp taken before its read: never bytes newer than what went in, which is how a
                # later poll comes back to a file it was told it had already handled.
                self._config_stamp = stamp
            return note

    def adopt_policy(self, text: str) -> str | None:
        """Put the policy written in `text` in force, without reading anything. Returns why this text
        can't be used - a config error or anything else that went wrong - or None that it was.

        The dashboard calls this with the same text it took the version from. What the page is shown
        is the rules and, beside them, the version of the file they came from, and it sends that
        version back when it saves: if the rules come from this process's memory and the version
        from a fresh read of the file, the pair stops meaning anything the moment the file is edited
        elsewhere. The page posts rules it never saw alongside a version that matches, the save goes
        through, and the hand-edit is written over with no 409 to say anyone else had an opinion.

        One turn of `_policy_lock` over the parse and the swap, and the version recorded afterwards:
        the rules in force belong to a named piece of text, and an install that didn't come from the
        file as it now is leaves the stamp clear, so the next look finds a file nobody installed.
        """
        with self._policy_lock:
            try:
                cfg = cfgmod.parse(text)
                changed = self._swap_policy(cfg)
            except Exception as e:  # noqa: BLE001 - a file we can't use isn't a reason to stop watching
                # Whatever it is - the validator missing something, a value the swap can't use, a bug
                # in the swap itself - it goes to the owner as `config_error` and the rules already in
                # force carry on. This is called from `probe()` and from the watcher's pass: letting it
                # out takes down the process that is looking after the card, which is the one thing a
                # bad config must never buy.
                return self._config_error(f"{self.config_path}: {type(e).__name__}: {e}")
            self._adopted = version_of(text)
            self._config_stamp = None       # whoever installs rules sends the next look back out
            if self.config_error is not None:    # the file reads again: say so once, the same way
                util.log("policy: the config reads again" + (f" - {changed}" if changed else ""))
            self.config_error = None
            if changed:
                util.log("policy: " + changed)
            return None

    def _swap_policy(self, cfg: dict) -> str:
        """The policy keys of a parsed config onto the live workloads. Returns what changed, in
        the words `status` uses, because an owner who saves a rule and looks at `status` a second
        later is checking the dashboard isn't showing them a stale answer."""
        wls = cfg.get("workloads") or {}
        notes = []
        for name, w in self.workloads.items():
            raw = wls.get(name)
            if not isinstance(raw, dict):
                continue          # a workload removed since this started: its rules stay
            yields_to = (tuple(raw["yields_to"]) if "yields_to" in raw
                         else policy.DEFAULT_YIELDS_TO)
            asks_for = (tuple(raw["asks_for"]) if "asks_for" in raw
                        else policy.DEFAULT_ASKS_FOR)
            priority = raw.get("priority", policy.DEFAULT_PRIORITY)
            if (w.yields_to, w.asks_for, w.priority) != (yields_to, asks_for, priority):
                notes.append(f"{w.label} gives way for {policy.describe(yields_to, asks_for)}"
                             f", priority {priority}")
            w.yields_to, w.asks_for, w.priority = yields_to, asks_for, priority
        self.pressure_cfg = pressure.cfg(cfg.get("pressure"))
        self.ask = policy.ask_cfg(cfg.get("ask"))
        return "; ".join(notes)

    def _config_error(self, message: str | None) -> str | None:
        """Remember what's wrong with the config, and say it once: a watcher that polls twice a
        second would otherwise bury every other line it writes."""
        if message and message != self.config_error:
            util.log(f"the config file no longer reads: {message} - the rules it had before are "
                     "still running")
        self.config_error = message
        return message

    # --- status --------------------------------------------------------------

    def probe(self) -> dict:
        """What each workload is doing, the card's readings, and who owns it.

        Also where a long-running process picks up a saved policy: `record`, the dashboard and its
        sampler all sample through here. Until the file changes it costs one `stat`, and for a
        command that read the config itself a moment ago it costs nothing - the stamp already
        matches.
        """
        self.reload_policy()
        # One card read, every process in it: the pressure reading needs the app that grew, which
        # is not necessarily among the eight biggest holders, and the report has to be built from
        # the same numbers the trigger would act on.
        gpu, every = self.snapshot_all(limit=0)
        held = every[:8]
        if "error" not in gpu:
            # Kept in the card's own block so the samples get it for free: a spill is the other half
            # of "the card was full", and it is the one number here that says the card ran out.
            gpu["shared_mib"] = self.shared_total_mib()
        states = {n: w.probe(gpu) for n, w in self.workloads.items()}
        active = [n for n, w in self.workloads.items() if w.active(states[n], gpu)]
        mode = "conflict" if len(active) > 1 else active[0] if active else NONE
        ram = self.machine_ram()
        ram.update({n: w.memory() for n, w in self.workloads.items()})
        hits = self._claims_in(held)
        status = {"mode": mode, **states, "gpu": gpu, "holders": held, "claims": hits,
                  "policy": {n: {"priority": w.priority, "yields_to": list(w.yields_to),
                                  "gives_way_for": policy.describe(w.yields_to, w.asks_for)}
                             for n, w in self.workloads.items()},
                  "pressure": self.pressure_status(card=(gpu, every)),
                  # When each was last seen working, and the questions the log shows as unanswered:
                  # the two things an owner needs to answer an Ask me - how recently the workload
                  # was in use, and what is being asked of them right now - beside the card they
                  # are deciding about.
                  "last_used": self.used_report(), "asks": self.open_asks(),
                  "notifications": self.notify.status(),
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
            f"{n} {w.priority} ({policy.describe(w.yields_to, w.asks_for)})" for n, w in self.workloads.items()))
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
        if open := pr.get("open"):            # The card is short right now for something vhotseat doesn't manage, and this says so in
            # the same words the release wrote: what took the memory, how far past its usual it is,
            # and what has come off and what is still holding because of it.
            bits = [pressure.describe(open)]
            if open.get("released"):
                bits.append("unloaded " + ", ".join(self.label(n) for n in open["released"])
                            + (f" ({open['freed_mib']} MiB)" if open.get("freed_mib") else ""))
            for w in open.get("waiting") or []:
                bits.append(f"{self.label(w['workload'])} keeps its model: {w['why']}")
            lines.append(f"{pad('pressure:')}" + "; ".join(bits))
        used = s.get("last_used") or {}
        if any(u.get("ago_s") is not None for u in used.values()):
            # Only when something has been recorded: a box where nothing has stamped yet says so
            # in the ask itself ("nothing has recorded it working since this started"), and a line
            # of "unknown, unknown" here is noise that teaches an owner to ignore the line.
            lines.append(pad("used:") + ", ".join(f"{n} {used[n]['phrase']}" for n in self.workloads
                                                 if used.get(n, {}).get("ago_s") is not None))
        if asks := s.get("asks"):
            # The open question, in the words it was asked in, with how long is left and what
            # happens without an answer: an owner who reads this in a terminal can answer it on the
            # dashboard from the same screen, and shouldn't have to guess which of the two it says.
            lines.append(pad("asks:") + "; ".join(
                f"{a['text']['headline']} - {a['label']}: "
                + (f"answer within {policy.clock_phrase(a['left_s'])}, " if a["left_s"] > 0 else "")
                + a["text"]["no_answer"]
                for a in asks)
                + ("" if (s.get("notifications") or {}).get("available") else
                   " (no notification from here: answer it on the dashboard)"))
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
                   gpu: dict | None = None, consent=()) -> list[str]:
        """Release every workload but `x`, restarting one whose release isn't
        enough when it allows that; then, if `admit`, restart idle ones while
        `x`'s admission isn't met. Returns what was done, for the event log.

        `consent` is the workloads the owner has already said to unload - the answer to an open
        ask about them (`ask_before_request`), which is the only way an `asks_for:` workload comes
        off the card. They are off the blocker list; everything else on it is either refused or
        still waiting for an answer, and refusing for those is the point.

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
            blockers = [n for n in self.wont_give_way(x.name, gpu) if n not in consent]
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

    def acquire(self, name: str, wait: float, load: bool, requested: bool = False,
                consent=()) -> list[str]:
        """Give the GPU to `name` (or to nobody, for "none"): release the others,
        start it if it's stopped, check its admission, and load it if `load`.
        `requested` is what a workload asked for on nobody's order (see _make_room), and
        `consent` is what an Ask me has already been answered to unload."""
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
                               requested=requested, gpu=s["gpu"], consent=consent)
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
        is short of VRAM" is otherwise a mystery. Each answer carries its own priority,
        yields_to and asks_for, so what to change is on the same line as what is wrong.
        """
        s = self.probe()
        gpu, ram = s["gpu"], s["ram"]
        hits = self.claim_hits()
        # Which pairs are already standing questions rather than decisions the config made, so the
        # note can say "answer it" next to the one the owner can still change their mind about.
        asked = {(a.get("workload"), a.get("requester")): a for a in s.get("asks") or []}
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
                if hits and held > y.idle_vram_mib and not policy.yields(y.yields_to, policy.CLAIMS,
                                                                        y.asks_for):
                    # The claim will leave this one alone, so the game gets less than the
                    # card could give it. Worth saying on the workload being asked about:
                    # "the game is short of VRAM" is otherwise a mystery.
                    notes.append(f"{y.label} keeps its model for the claim: "
                                 f"{policy.why_not(y.yields_to, y.asks_for, policy.CLAIMS, 'a claim on the card')}")
                    if (open := asked.get((y.name, policy.CLAIMS))) is not None:
                        # The difference between a refusal and a question, said where the owner can
                        # act on it: this one is still winnable, and from the dashboard.
                        notes[-1] += f" - asked already (ask {open['id']}): answer it on the dashboard"
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
                    if not policy.yields(y.yields_to, name, y.asks_for):
                        would = "blocked"
                        notes.append(f"{y.label} holds {held} MiB and won't give way for {x.label}: "
                                     f"{policy.why_not(y.yields_to, y.asks_for, name, 'it')}")
                        if (open := asked.get((y.name, name))) is not None:
                            notes[-1] += (f" - asked already (ask {open['id']}): {x.label} is waiting "
                                          "for the answer, not refusing")
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
                                 "workload: it has not come off yet"
                                 + (f" - asked already (ask {asked[(name, policy.PRESSURE)]['id']}): "
                                    "answer it on the dashboard"
                                    if (name, policy.PRESSURE) in asked else ""))
                elif policy.may_ask(x.yields_to, x.asks_for, policy.PRESSURE):
                    notes.append("it is a pressure target only once you answer the ask about it: "
                                 f"{policy.why_not(x.yields_to, x.asks_for, policy.PRESSURE, 'pressure')}")
                elif not policy.yields(x.yields_to, policy.PRESSURE, x.asks_for):
                    notes.append(f"it was never a pressure target: "
                                 f"{policy.why_not(x.yields_to, x.asks_for, policy.PRESSURE, 'pressure')}")
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
                         "priority": x.priority, "yields_to": list(x.yields_to),
                         "asks_for": list(x.asks_for), "notes": notes}
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
        # A hook runs because something asked this workload for an answer, which is the fact the
        # next Ask me will be asked with. Whether the load is allowed or refused is a separate
        # question: an agent turned away is still an agent that will ask again in a moment.
        self.note_used(name)
        # The asking first, and before the lock: while the owner decides, the card has to stay
        # free for whatever else is running, and a hook that held the GPU lock for two minutes
        # waiting for a person would stop every other handover on the box with it. Nothing is
        # asked while another actor holds the lock: that path answers from the note, as it always
        # has, and that actor is deciding this same card right now.
        # The hook's whole run - the asking, and the handover the answer authorises - has to fit in
        # the workload's own limit (`gate.timeout`), so the limit is taken once, here. Measured twice
        # (one reading for the asking, another started when the answer arrives) it hands the handover
        # a budget the host stopped paying for: the restart guard inside it is told a whole timeout
        # is left when the waiting spent it, and a restart cut off in the middle of a killed hook is
        # worse than a refusal - the service is off, the model isn't loaded, and the agent retries.
        deadline = ((time.monotonic() + x.gate["timeout"] * util.SCALE) if x.gate.get("timeout")
                    else None)
        consent: set[str] = set()
        if not self.lock.holder():
            consent, blockers, why = self.ask_before_request(name, own_deadline=deadline, source="gate")
            if why:
                self._policy_refused("gate", name, blockers, why)
                self.events.record(kind, source="request", allowed=False, reason=why)
                util.log(f"refusing {x.label} load: {why}")
                return False
        got = self.lock.try_take(f"gate {name}")
        if not isinstance(got, Held):
            holder = got or "a switch"
            allowed = got in (f"switch {name}", f"hold {name}")
            self.events.record(kind, source="hook", allowed=allowed, reason=f"{holder} in progress")
            if not allowed:
                util.log(f"refusing {x.label} load: {holder} is in progress")
            return allowed

        # The whole hook - the asking included - runs against the deadline taken above.
        with got:
            gpu = self.snapshot()
            hits = self.claim_hits()
            reason = (gpu.get("error") or self.claim_refusal(hits)
                      or next((r for y in others if (r := y.busy())), None))
            if not reason:
                # Already loaded (a hook fired again, or `vhotseat gate` run by hand):
                # its own memory isn't a shortfall, so don't restart anyone for it.
                resident = x.active(x.probe(gpu), gpu)
                try:
                    done = self._make_room(x, 0, self.gate_release_timeout_s, ensure=False,
                                           deadline=deadline, admit=not resident, requested=True,
                                           gpu=gpu, consent=consent)
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

        A workload whose `yields_to:` doesn't cover `claims` and whose `asks_for:` doesn't name
        them isn't on that list whatever it
        holds: the game is short of that much VRAM and vhotseat says so rather than unloading
        something the owner refused to move for a game. It is recorded as kept
        (`policy-kept`), not as waiting - nothing is waiting for it, it was never owed.

        A workload that wants being *asked* is on it: the claim is owed that release too, and what
        it isn't owed yet is the answer. It is asked once, released as soon as the answer says
        unload, and recorded as kept if the answer says keep (`ask_before_release`). The game runs
        meanwhile either way: a claim can't be refused, which is what makes asking it safe.

        This is decided once, when the claim's release first runs, and the answer goes in the
        event log (`claim-release-targets`). It matters because a claim lasts as long as the
        game does, and a typed `vhotseat switch strata` in the middle of it is an order: what
        the owner loads afterwards is theirs, and a watcher that read the card fresh every poll
        would undo it every 2 seconds for the rest of the game."""
        snap = self.snapshot()
        owed = [n for n, y in self.workloads.items()
                if (snap.get(f"{n}_mib") or 0) > y.idle_vram_mib
                and (policy.yields(y.yields_to, policy.CLAIMS, y.asks_for)
                     or policy.may_ask(y.yields_to, y.asks_for, policy.CLAIMS))]
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
        cands = {n: y for n, y in cands.items()
                 if policy.yields(y.yields_to, policy.CLAIMS, y.asks_for)
                 or policy.may_ask(y.yields_to, y.asks_for, policy.CLAIMS)}
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
            cands = self.ask_before_release(policy.CLAIMS, cands, want, source="claim",
                                            name=claim.get("name"), note=claim.get("note"),
                                            pid=claim.get("pid"), snap=snap,
                                            scope=self.claim_scope(claim.get("pid")))
            if not any(want.get(n) for n in cands):
                # Everything it is still owed wants being asked first, and the answers haven't
                # come. Nothing was released and nothing is `waiting`: the game is running with
                # what is free, and the question - not a busy workload - is what's owed an answer.
                return {"declined": None, "by_actor": False, "would": sorted(want), "released": [],
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

    def _sample_read(self, method: str, *args):
        """One number from the telemetry, or (None, what went wrong). The samples are an optional
        feature: a database another process has locked, or a file that has gone corrupt, is not a
        reason for `status`, a switch or the Strata gate to fail. Whoever reads it decides what the
        missing number means - for the pressure trigger it is "no usable samples", and it stops."""
        if self.telemetry is None:
            return None, None
        try:
            return getattr(self.telemetry, method)(*args), None
        except READ_ERRORS as e:
            return None, str(e) or type(e).__name__

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

        A telemetry file that can't be read is the same answer as no samples in it, with the error
        named: the history is optional, and every probe comes through here, so a locked database may
        not take `status`, a switch and the gate down with it.
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
        floor, unreadable = self._sample_read("floor_mib")
        if unreadable:
            return {**out, "why": f"no usable samples: the telemetry couldn't be read ({unreadable})"}
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
        low, unreadable = self._sample_read("below_free", cfg["min_free_mib"],
                                            max(2 * cfg["sustain_s"], 120))
        if unreadable:
            return {**out, "why": f"no usable samples: the telemetry couldn't be read ({unreadable})"}
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
        would give up: only ones whose `yields_to:` covers `pressure`, or whose `asks_for:` asks
        about it - which makes it a target the episode is owed as soon as the owner answers - and
        only ones holding more
        than their idle weight, because the app that grew is short of VRAM and not of the RAM a model
        with no card weights costs.

        A busy one stays on the list. It is left alone when the release runs and asked again when it
        goes idle - work in flight is never interrupted, whatever asked for the card - and dropping it
        here would mean an episode quietly freeing less than it decided to. So would dropping an
        asked-for one: the need was worked out with its memory on the card, and taking it out here
        would have the episode pay for less than it asked for while the question is open, then
        start a second episode for the rest once the answer comes.
        """
        snap = snap if snap is not None else self.snapshot_all()[0]
        out = []
        for n, y in sorted(self.workloads.items(), key=lambda kv: (kv[1].priority, kv[0])):
            held = (snap.get(f"{n}_mib") or 0) - y.idle_vram_mib
            if held > 0 and (policy.yields(y.yields_to, policy.PRESSURE, y.asks_for)
                             or policy.may_ask(y.yields_to, y.asks_for, policy.PRESSURE)):
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
        other finds nothing left holding, writes nothing, and declines as `by_actor`. So is the
        episode check: both can be told "nothing is open" in the same moment before either has
        written a row, and the one that arrives second at the lock has to find the first one's.

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
            blocked = self.pressure_can_start()       # the log as whoever held the lock left it
            if blocked:
                # The other actor got its row down while this one was queueing for the lock. It is
                # the one doing this release: say so quietly rather than write a second episode over
                # the same memory and ask for the same workloads a second time.
                return _declined(blocked, by_actor=self.events.episode_open() is not None)
            readings = self.pressure_readings(claimed)      # the card as whoever held the lock left it
            if readings.get("why"):
                return _declined(readings["why"])
            grower = readings.get("grower") or {}
            self.events.record("pressure", id=uuid.uuid4().hex[:12], source=source,
                               name=grower.get("name") or name,
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
                 if n in targets and (policy.yields(y.yields_to, policy.PRESSURE, y.asks_for)
                                      or policy.may_ask(y.yields_to, y.asks_for, policy.PRESSURE))}
        want = {n: max(0, (snap.get(f"{n}_mib") or 0) - y.idle_vram_mib) for n, y in cands.items()}
        if not any(want.values()):
            return {"declined": None, "by_actor": False, "would": sorted(cands), "released": [],
                    "waiting": [], "freed_mib": 0}
        name = (ep.get("episode") or {}).get("name")
        episode = ep.get("episode") or {}
        cands = self.ask_before_release(policy.PRESSURE, cands, want, source=source, name=name,
                                        since=episode.get("time"), snap=snap,
                                        scope=self.episode_scope(episode))
        if not any(want.get(n) for n in cands):
            # Owed only workloads whose answer hasn't come: the card stays short, the question is
            # open, and nothing is waiting on an unload nobody agreed to.
            return {"declined": None, "by_actor": False, "would": sorted(cands), "released": [],
                    "waiting": [], "freed_mib": 0}
        released, waiting = self._release_cands(cands, want)
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
        if request:
            claim = self.claim_refusal()
            if claim and self.probe()["mode"] != name:
                self.events.record("switch", to=name, ok=False, error=claim)
                util.log(f"switch to {name} refused: {claim}")
                return {"ok": False, "error": claim}
        consent: set[str] = set()
        if request:
            claim = self.claim_refusal()
            if claim and self.probe()["mode"] != name:
                self.events.record("switch", to=name, ok=False, error=claim)
                util.log(f"switch to {name} refused: {claim}")
                return {"ok": False, "error": claim}
            # A workload the policy wants asked about first is not refused: the request waits for
            # the answer, up to the ask's own timeout, and then proceeds or refuses per the answer
            # or the fallback. Before any waiting and outside the lock: while the owner decides,
            # the card must stay free for whatever else is running, and a guard's question must
            # not cost the box a handover it can't get while the answer is outstanding.
            consent, blockers, why = self.ask_before_request(name, source="guard")
            if why:
                e = PolicyRefused(why, workloads=blockers)
                self._policy_refused("switch", name, blockers, why)
                self.events.record("switch", to=name, ok=False, error=why)
                util.log(f"switch to {name} refused: {why}")
                return {"ok": False, "error": why, "exception": e}
            # The claim, again: waiting for an answer is two minutes of somebody else's time, and a
            # game can start inside it. What the owner agreed to unload is unloaded for the game
            # that is on the card now, not the one that was running when the question was opened -
            # and if there is one now, this is the same refusal the request would have got had it
            # arrived a minute later, which is the only reason it was waiting.
            claim = self.claim_refusal()
            if claim and self.probe()["mode"] != name:
                self.events.record("switch", to=name, ok=False, error=claim)
                util.log(f"switch to {name} refused: {claim}")
                return {"ok": False, "error": claim}
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
                    done = self.acquire(name, left, load=load, requested=request,
                                        consent=consent)
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
