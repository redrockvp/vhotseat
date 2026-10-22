"""Pressure: a process vhotseat doesn't manage taking the card.

A game announces itself: it is a process a claim rule names, and phase 1 releases the card for it.
A Blender render, a CUDA script, a browser that decides to use the GPU for something large, does
not. Nobody asked vhotseat for anything, and no rule named the process, yet the card is short
because of it and the models that are loaded are what's in the way. Pressure is how vhotseat
notices that: the card has been out of working room for a while, and the memory it can't manage
is far above what it usually carries.

Three rules keep it from becoming the thing that ruins the owner's afternoon.

**Growth is a fact, not an order.** Pressure justifies releasing workloads that are idle. It never
refuses a load: an app vhotseat doesn't manage getting big is not a reason to say no to something
the owner typed, any more than a claim refusing a release would be. And it never chases a model the
owner loads afterwards - the workloads an episode is owed are decided once, at its start, and
recorded.

**Asymmetric hysteresis, on purpose.** The trigger asks for a low card sustained over several
samples *and* the unmanaged memory well above its usual floor. Putting a workload back asks for
more than that, and an episode cannot follow the last one more closely than `cooldown_s`. The cost
of getting this wrong is not a moment of thrash: it is unloading ~20 GB, reloading ~20 GB, and a
server that may need a restart. On this box a 24 GB card with a 21 GB model has under 1 GB of
slack, so there is barely a band to oscillate in: few, large, deliberate evictions beat continuous
trimming.

**The floor is measured, not guessed.** dwm, the browser, Orca and the guard node's CUDA context
hold MiB nothing here can ever evict: 3.1 GiB on this box, steady within 173 MiB across three
minutes. vhotseat uses the median of what it has recorded (Telemetry.floor_mib()) both for the
dashboard's "fits alongside" and for "how much of the card has grown", so it does not go hunting
for memory that does not exist - and so a game that ran last evening, whose memory came back when
it closed, is not counted as a permanent floor either.

Shared GPU memory (system RAM the card borrows when it runs out) is recorded as evidence that the
card was oversubscribed at that moment. It is never a trigger: on this box 37 processes report
51.0 GiB of it between them - 50.9 GiB of it Strata - while the card is simply full.
"""

import fnmatch
import os

DEFAULTS = {
    # Measured on this box 2026-10-22 (99 samples over ~200 s: Strata loaded with a 21394 MiB model,
    # ComfyUI down, browser/Orca/Discord open). free 779-958 MiB; non-workload dedicated
    # 3096-3269 MiB (dwm 1370, firefox 372, explorer 362, Discord 211, Orca ~186). The defaults are
    # deliberately slow to anger: a false positive costs an unload and reload of a ~20 GB model.
    "enabled": True,           # the escape hatch: this whole trigger, off
    "min_free_mib": 512,       # below the machine's ordinary bottom (779), so "low" means a change
    "sustain_s": 30,           # and it stayed there: 6 samples at the default 5 s interval
    "grow_mib": 2000,          # unmanaged memory this far over its usual floor: 10x the 173 MiB of
                               # jitter the ordinary desktop produces
    "headroom_mib": 1000,      # release enough to get back to min_free plus this, not to the line
    "end_margin_mib": 2000,    # an episode ends only once free is back above min_free plus this
    "cooldown_s": 600,         # and the next one starts no sooner than this after the last
    "ignore": (),              # names/globs that are never growers: a VM, a benchmark, a miner
}
KEYS = tuple(DEFAULTS)

# The rows that say an episode is open and what has been done about it. `pressure` is the episode:
# written once, with the grower, the readings that justified it and the workloads it was owed.
EPISODE_ROWS = ("pressure", "pressure-release", "pressure-release-waiting", "pressure-end")


def cfg(raw: dict | None) -> dict:
    """The `pressure:` block with the defaults filled in. Kept as a plain dict, the way the other
    blocks are, so the dashboard can show it and a later UI can edit it."""
    raw = raw or {}
    return {k: (list(raw[k]) if k == "ignore" and raw.get(k) else raw.get(k, v))
            for k, v in DEFAULTS.items()}


def ignores(extra=()) -> frozenset:
    """Names and path globs that are never growers. A benchmark, a VM with a GPU passthrough and a
    mining job all legitimately want the whole card: the owner who sets `pressure.ignore:` has said
    so, and vhotseat must not unload a working model to hand them more room."""
    return frozenset(_fold(s) for s in (extra or ()))


def _fold(s: str | None) -> str:
    """Windows names and paths are case-insensitive; fnmatch isn't, so both sides get folded."""
    return s.lower() if (s and os.name == "nt") else (s or "")


def ignored(name: str | None, exe: str | None, ignore: frozenset) -> bool:
    """Is this process one the owner has said is never a grower?"""
    folded, folded_exe = _fold(name or ""), _fold(exe or "")
    return any(fnmatch.fnmatch(folded, pat) or (folded_exe and fnmatch.fnmatch(folded_exe, pat))
               for pat in ignore)


def growers(rows: list[dict], ignore: frozenset = frozenset(), claimed: set = frozenset()) -> list[dict]:
    """The processes that could be the reason the card is short, biggest first.

    `rows` is the holder list from one card read: processes attributed to a configured workload are
    not growers, because a workload's own growth is the gate's business and vhotseat can ask it to
    give the memory back. `claimed` are the pids a claim rule already covers: a game is handled by
    the claim (phase 1), not twice over. `ignore` is the owner's own exclusion list.
    """
    return [r for r in sorted(rows, key=lambda r: -(r.get("mib") or 0))
            if r.get("mib") and not r.get("workload") and r.get("pid") not in claimed
            and not ignored(r.get("name"), r.get("exe"), ignore)]


def grow_mib(rows: list[dict], floor: int | None, ignore: frozenset = frozenset(),
             claimed: set = frozenset()) -> int:
    """How far the memory vhotseat doesn't manage stands above its usual floor right now.

    Not how much the card has: the card being full of our own models is not pressure, and neither
    is the desktop sitting at its ordinary 3 GiB. A claimed process's memory is out of the total
    too - the claim is what handles a game, and its memory came from the same card the claim
    already asked for.
    """
    unmanaged = sum(r["mib"] for r in growers(rows, ignore, claimed))
    return max(0, unmanaged - (floor or 0))


def need_mib(free: int, min_free: int, headroom: int) -> int:
    """How much has to come back for the episode to be worth having: to `min_free` plus the
    headroom, not to the line it tripped over."""
    return max(0, min_free + headroom - free)


def targets(candidates: list[tuple[str, int]], need: int) -> list[str]:
    """The workloads to release: the shortest run down the list that pays for it.

    `candidates` is [(workload, MiB it would give up)] already filtered - to the ones that yield to
    pressure, hold something, and aren't working - and already ordered lowest priority first. That
    ordering is the owner's, so the answer is always a run from the front of it, and nothing is
    taken once the need is covered: two models unloaded where one paid for it is a reload the owner
    never asked for. A workload holding nothing is skipped - the app that grew is short of VRAM, not
    of the RAM a model with no card weights costs.

    It does not reorder to find one big release instead of two small ones. `priority:` says which
    workloads the owner would rather lose; swapping a cheap one for an expensive one to save a
    release would be reopening a question they already answered.
    """
    chosen, total = [], 0
    for name, mib in candidates:
        if total >= need or mib <= 0:
            continue
        chosen.append(name)
        total += mib
    return chosen


def describe(ep: dict) -> str:
    """One line saying what an episode was: who grew, by how much, and what was given up."""
    if not ep:
        return ""
    g = ep.get("episode") or {}
    bits = [f"{g.get('name') or 'a process'} holds {g.get('other_mib', 0)} MiB vhotseat doesn't "
            f"manage, {g.get('grew_mib', 0)} MiB over its usual {g.get('floor_mib', 0)}"]
    if g.get("free_mib") is not None:
        bits.append(f"card at {g.get('free_mib')} MiB free for {int(g.get('low_for_s') or 0)} s")
    if g.get("shared_mib"):
        bits.append(f"{g['shared_mib']} MiB shared (system RAM the card borrowed)")
    return ", ".join(bits)
