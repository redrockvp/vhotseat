"""Claims: a process that owns the card because it is the thing a person is using.

A workload is something vhotseat can *ask* to release VRAM. A game isn't: it has no
API to call, and killing it is not vhotseat's call to make. What vhotseat can do is
stop putting anything on top of it. While a claimed process runs, no new model loads
and no new render starts, and a handover that would have made room for one isn't
taken. Whatever is already loaded keeps what it has: a render in flight is never
interrupted (docs/how-it-works.md §3.2), so the game waits out that clip exactly as a
`switch` would. That is the whole of it - the loads are the lever, not the memory.

What counts as a claim is deliberately narrow, because the wrong rule is worse than
no rule: a launcher that runs all day (Steam, Epic, Battle.net, EA) is not a game
being played. What claims the card is the game process itself - known by the
directory it runs from, by a launcher being its parent, or by a rule you write. A
process vhotseat manages is never a claim: the workloads in the config are excluded.
"""

import fnmatch
import os

# Built-in rules: game processes, not launchers. A path rule reads the image path,
# which is None for a process running as another user or elevated, so a `name` rule
# (a game known by its exe) is the one that survives that.
WINDOWS_GAMES = (
    {"path": r"*\steamapps\common\*", "note": "a Steam game"},
    {"path": r"*\Epic Games\*", "note": "an Epic Games game"},
    {"path": r"*\Riot Games\*", "note": "a Riot Games game"},
    {"path": r"*\Games\*", "note": "a game in a Games folder"},
    {"name": "RobloxPlayerBeta.exe", "note": "Roblox"},
    {"name": "EldenRing.exe", "note": "Elden Ring"},
)
POSIX_GAMES = (
    {"path": "*/steamapps/common/*", "note": "a Steam game"},
    {"path": "*/Games/*", "note": "a game in a Games folder"},
    {"name": "RobloxPlayerBeta.exe", "note": "Roblox"},
)

# The launchers and their helpers: they run all day, and none of them is a game being
# played, so they never claim the card - whatever directory they happen to live in.
# A rule of one's own can still name one; this only stops the built-ins doing it.
LAUNCHERS = (
    "steam.exe", "steamwebhelper.exe", "steamerrorreporter.exe", "steamservice.exe",
    "EpicGamesLauncher.exe", "EpicWebHelper.exe", "EAAntiCheatService.exe",
    "EADesktop.exe", "EALaunchHelper.exe", "RiotClientServices.exe", "Battle.net.exe",
    "Agent.exe", "upc.exe", "UplayCrashHandler.exe", "RobloxBrowserBootstrap.exe",
    "steam", "steam.sh", "steamwebhelper",
)

KEYS = ("name", "path", "parent", "note")
_LAUNCHERS = frozenset(s.lower() for s in LAUNCHERS)


class Rule:
    """One claim rule: match a process by exe name, image path, or the name of any
    ancestor process. `note` says what it means to have matched, in the report."""

    def __init__(self, raw: dict):
        bad = [k for k in raw if k not in KEYS]
        if bad:
            raise ValueError(f"claim rule: unknown key{'s' if len(bad) > 1 else ''} "
                             f"{', '.join(sorted(bad))}; expected {', '.join(KEYS)}")
        if not any(raw.get(k) for k in ("name", "path", "parent")):
            raise ValueError("claim rule: needs at least one of name, path, parent")
        self.name = str(raw.get("name") or "")
        self.path = str(raw.get("path") or "")
        self.parent = str(raw.get("parent") or "")
        self.note = str(raw.get("note") or "")

    def __repr__(self) -> str:
        return f"Rule({self.name or self.path or self.parent})"


def rules(cfg_rules=(), builtin: bool = True) -> list[Rule]:
    """The rules in force: built-ins for this platform first, then the config's."""
    out = []
    if builtin:
        builtins = WINDOWS_GAMES if os.name == "nt" else POSIX_GAMES
        out += [Rule(r) for r in builtins]
    out += [Rule(r) for r in cfg_rules or ()]
    return out


def _fold(s: str | None) -> str:
    """Windows paths and exe names are case-insensitive; fnmatch isn't, so fold both
    sides of every comparison there."""
    return s.lower() if (s and os.name == "nt") else (s or "")


def ancestors(pid: int, procs: dict[int, dict], depth: int = 12) -> list[str]:
    """The names of `pid`'s parent chain, nearest first. Bounded, and cycle-safe: a
    snapshot taken while processes die can point a parent at a pid already reused."""
    out, seen = [], {pid}
    for _ in range(depth):
        p = procs.get(pid)
        if p is None or p.get("ppid") in (None, 0) or p["ppid"] in seen:
            break
        pid = p["ppid"]
        seen.add(pid)
        parent = procs.get(pid)
        if parent is not None and parent.get("name"):
            out.append(parent["name"])
    return out


def matching(rs: list[Rule], procs: dict[int, dict], exclude: set = frozenset()) -> list[dict]:
    """Every running process a rule claims: [{pid, name, exe, note, by}].

    `exclude` is the set of pids already attributed to configured workloads: the thing
    holding the card *for* vhotseat is never also the thing that owns it, and a
    workload's directory isn't guaranteed to sit outside a Games folder.
    """
    hits = []
    for pid, p in procs.items():
        name, exe = p.get("name") or "", (p.get("exe") or "")
        if not name or pid in exclude or _fold(name) in _LAUNCHERS:
            continue
        chain = None
        for r in rs:
            if r.name and fnmatch.fnmatch(_fold(name), _fold(r.name)):
                by = f"{name}"
            elif r.path and exe and fnmatch.fnmatch(_fold(exe), _fold(r.path)):
                by = f"{name} ({exe})"
            elif r.parent:
                if chain is None:
                    chain = ancestors(pid, procs)
                if not any(fnmatch.fnmatch(_fold(a), _fold(r.parent)) for a in chain):
                    continue
                by = f"{name}, launched by {next(a for a in chain if fnmatch.fnmatch(_fold(a), _fold(r.parent)))}"
            else:
                continue
            hits.append({"pid": pid, "name": name, "exe": exe or None,
                         "note": r.note or "claimed", "by": by})
            break                       # one rule per process: the first match explains it
    return hits


def describe(hits: list[dict], limit: int = 3) -> str:
    """What the claims add up to, grouped: a browser and a game launcher start twenty
    processes, and twenty clauses explain nothing. `firefox.exe x20 (a Steam game)`."""
    if not hits:
        return ""
    groups: dict[tuple, int] = {}
    for h in hits:
        groups[(h["name"], h["note"])] = groups.get((h["name"], h["note"]), 0) + 1
    bits = [f"{name}" + (f" x{n}" if n > 1 else "") + f" ({note})"
            for (name, note), n in list(groups.items())[:limit]]
    if len(groups) > limit:
        bits.append(f"and {len(groups) - limit} more")
    return ", ".join(bits)
