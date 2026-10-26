"""Who may ask a workload to let go of its models, and in what order.

Unloading used to be one rule for everything: whatever held memory on the card was a
candidate for every handover. `yields_to:` makes it a per-workload decision - for each
loaded workload, which newcomers may unload it and which are refused instead. An entry
is another workload's name, `claims` (a process that owns the card by right, see
claims.py), `pressure` (the card running short because of a process vhotseat doesn't
manage), or `*` for anyone. The default when the key is absent is `["*"]`, which is
exactly the old behaviour, and an empty list means it gives way for nobody: only a
typed `vhotseat switch` moves it.

`asks_for:` is the third answer a person has in mind between "make room" and "keep
loaded": unload me only after asking. It takes the same entries as `yields_to` except the
`*` - an ask has to name who is asking - and an entry named in both keys is a config error
rather than a rule with two readings. An `ask` is a row in the event log (`ask`, closed by
`ask-answer`) and a question in front of the owner - a notification and the dashboard - and
what happens next depends on who was asking: a workload waits for the answer, a claim or an
episode runs meanwhile and is owed the release as soon as the answer says unload.

`ask:` (top level) is what an unanswered ask is worth: `timeout_s` to wait, and `fallback`
of `keep` or `release` for what to do when nobody answers within it. Both go into the `ask`
row itself, so whoever is waiting - this process, another one, or whichever watcher notices
the timeout first and applies the fallback - is waiting on the numbers the question was
asked with, not on a config that may have been edited since.

`priority:` (0-100, default 50) is the order among the workloads that *may* be unloaded:
the lowest goes first. It never overrules `yields_to` - a workload that refuses is not
unloaded more slowly, it is not unloaded.

The vocabulary lives here, in one place, so that the dashboard or tray UI that edits it
later (drag to reorder, a checkbox per newcomer) changes the same two values the arbiter
reads rather than inventing a second meaning for them.
"""

# The entries in `yields_to:` that aren't a workload's name.
CLAIMS = "claims"
PRESSURE = "pressure"
ANYONE = "*"
SPECIAL = (CLAIMS, PRESSURE, ANYONE)
# A workload may not be named one of these: inside `yields_to:` the name would then
# mean two different things, and a config could state a rule that reads one way and
# acts another. (As with "none", which is already the card's owner when nobody is.)
RESERVED = (CLAIMS, PRESSURE)
DEFAULT_PRIORITY = 50
PRIORITY_MIN, PRIORITY_MAX = 0, 100


def priorities_for(order) -> dict:
    """The unload order an owner dragged into shape, as the `priority:` numbers the config
    holds. The arbiter asks the *lowest* priority to let go of its models first, so the first
    workload of the order gets the lowest number - the same order `GET /api/policy` reports, so
    an editor that reads the order and posts it back writes the same numbers it was shown.

    The ends sit at 10 and 90 rather than 0 and 100 so that putting one workload above or below
    the rest later doesn't mean renumbering the file, and so that the middle of a three-workload
    order lands exactly on the default 50 - which the writer then leaves out of the file rather
    than writing a default out. Ties keep the config's own order, which is what `priority:`
    already does."""
    n = len(order)
    if n <= 1:
        return {name: DEFAULT_PRIORITY for name in order}
    return {name: round(10 + i * (80 / (n - 1))) for i, name in enumerate(order)}

DEFAULT_YIELDS_TO = ("*",)
# `asks_for:` has no `*`: "ask me before unloading me for anyone" is not a question anyone
# can answer, and an absent key means it asks for nobody, which is the old behaviour.
SPECIAL_ASK = (CLAIMS, PRESSURE)
DEFAULT_ASKS_FOR = ()

# What an unanswered ask is worth (`ask:`).
ASK_DEFAULTS = {"timeout_s": 120, "fallback": "keep"}
FALLBACKS = ("keep", "release")
# The two answers an ask takes, and who gave it: the owner, or nobody answering in time. Named
# as well as tupled, because a caller reading `ANSWERS[0]` is guessing at which is which.
RELEASE, KEEP = ANSWERS = ("release", "keep")
ANSWERED_BY = ("owner", "timeout")

# How the special words read in a sentence shown to a person.
PHRASE = {CLAIMS: "a claim on the card", PRESSURE: "the card running short"}

# The two requesters that aren't a workload, as the editor shows them: what a column is
# called, and the line under it that says who that is.
SPECIAL_REQUESTERS = ((CLAIMS, "A game", "or any app that claims the GPU"),
                      (PRESSURE, "Low VRAM", "another app squeezes the card"))
# ...and how each one reads in the middle of a sentence, which is neither of those: the heading
# names the column and the line under it says who it is, while a row summary and a control's
# aria-label have to fit the words they sit between ("makes room for a game", "stays loaded for
# low VRAM").
SUMMARY_PHRASE = {CLAIMS: "a game", PRESSURE: "low VRAM"}


def ask_cfg(raw: dict | None) -> dict:
    """The `ask:` block with the defaults filled in, as a plain dict the way the other
    blocks are, so the dashboard can show it and the asking can be written against it.
    The two numbers are copied into the `ask` row when it opens, which is what any later
    reader waits on: a question asked at 2 min is still a 2 min question if the owner has
    since written 10 min into the file."""
    raw = raw or {}
    return {k: raw.get(k, v) for k, v in ASK_DEFAULTS.items()}


def yields(spec, reason: str, asks_for=()) -> bool:
    """May a workload with this `yields_to:` and `asks_for:` be unloaded for `reason` - a
    workload's name, `claims` or `pressure` - *on the config's own authority*? An `ask` says
    no: that workload may be unloaded too, but only once the owner has said so, and their
    answer is a second fact read from the event log, never one invented here."""
    return choice(spec, asks_for, reason) == "yes"


def may_ask(yields_to, asks_for, reason: str) -> bool:
    """Is this pair the one that asks the owner first? Stated once so that the release paths
    and the refusal paths can't disagree about which pairs are the asking ones: one of them
    opens a question, the other refuses, and a pair that is in neither (or in both) is the
    bug where a workload is unloaded without being asked."""
    return choice(yields_to, asks_for, reason) == "ask"


# The three answers one cell of the grid holds, in the order the editor lists them: what the
# newcomer gets, whether the owner is asked first, and nothing.
CHOICES = ("yes", "ask", "keep")


def choice(yields_to, asks_for, reason: str) -> str:
    """What one pair holds: "yes" (make room), "ask" (ask me first) or "keep" (neither).

    One function because the grid, the config and the arbiter must not each decide which of
    the two keys wins. A name in both is a config error, so the only overlap left is `"*"`
    in `yields_to:` facing a named ask, and there the narrower answer is the one that counts.
    `ask` is not consent to unload: everything that decides *by the config alone* calls
    `yields`, which answers no for it, and the consent that does unload it is the owner's
    answer, in the log. So a cell set to Ask me never unloads anything on its own and never
    refuses anything on its own either: it is a question, and the answer to it is a row."""
    if reason in asks_for:
        return "ask"
    return "yes" if ANYONE in yields_to or reason in yields_to else "keep"


def describe(spec, asks_for=()) -> str:
    """What a workload gives way for, as the words a status line, a refusal or the
    dashboard use: "anything", "nothing", or the list with the special words spelled
    out - and, for the pairs that want asking first, named as the question it is."""
    out = ("anything" if ANYONE in spec else "nothing" if not spec else names(spec))
    return f"{out}, and asks {names(asks_for)} first" if asks_for else out


def names(spec) -> str:
    """A list of policy entries as a person reads it: "a claim on the card", "comfy"."""
    return ", ".join(PHRASE.get(s, s) for s in spec)


def why_not(yields_to, asks_for, reason: str, who: str) -> str:
    """Why a workload is not being unloaded for `reason`, as the clause in the middle of the
    sentence the refusal, the kept row and `why` all say: the key to change and what it says
    about this particular newcomer. `who` is how that sentence names the newcomer, because
    "ComfyUI" and "a claim on the card" are different readings of the same entry.

    For an `ask` pair it says the answer hasn't come, which is the truth whether the question
    has been asked and is still open or hasn't been asked yet: in both cases what the owner can
    do about it is answer it, and what the arbiter is doing is waiting rather than deciding."""
    if choice(yields_to, asks_for, reason) == "ask":
        return f"its asks_for asks {who} first, and the answer hasn't come yet"
    return f"its yields_to ({describe(yields_to)}) doesn't cover {who}"


def answered_why(who: str, answer: str, by: str = "owner") -> str:
    """The same clause once an answer exists: whose call it was. An owner who clicked Keep has
    to read the next refusal as their own decision and not as a rule they forgot they wrote,
    and a fallback that fired while nobody was awake has to be named as that, because it is the
    one unload in the log nobody agreed to."""
    if by == "timeout":
        return f"nobody answered in time, so its ask: fallback ({answer}) decided"
    return (f"its asks_for asks {who} first, and you chose to "
            + ("keep it loaded" if answer == "keep" else "unload it"))


def used_phrase(ago_s: float | None) -> str:
    """When a workload was last seen working, in the words the notification and the dashboard
    ask their question with: "used 40 s ago", "idle for 25 min".

    No record is said as no record, never as idle. The whole point of the sentence is that the
    owner decides whether to pause their work first; a workload whose activity was never
    recorded being shown as "idle" is exactly how an agent gets unloaded between two steps."""
    if ago_s is None:
        return "nothing has recorded it working since this started"
    s = int(round(ago_s))
    if s < USED_RECENTLY_S:
        return f"used {s} s ago"
    if s < 5400:
        return f"idle for {s // 60} min"
    return f"idle for {s // 3600} h"


# Under this, a workload was seen working moments ago: recent enough that the next thing it
# does may already be on its way. It is the line `used_phrase` switches its wording on, and the
# one the editor's "try it" outcome uses to say "this may be mid-task" rather than "it is idle",
# so the two can't drift into disagreeing about the same stamp.
USED_RECENTLY_S = 90


def wait_phrase(timeout_s: float) -> str:
    """How long an answer has, in the sentence that says what happens without one: "2 min",
    "45 s". The form a person reads out loud, not a stopwatch."""
    s = int(round(timeout_s or 0))
    return f"{s // 60} min" if s >= 90 else f"{s} s"


def clock_phrase(seconds: float) -> str:
    """A length of time as a countdown: "45 s", "1:42", "48 min". What the question shows for
    "answer within": "102 s" makes someone do arithmetic and "1.7 min" makes them guess."""
    s = max(0, int(round(seconds or 0)))
    if s >= 3000:
        return f"{s // 60} min"
    return f"{s // 60}:{s % 60:02d}" if s >= 60 else f"{s} s"


def says(c: str, holder: str, requester_id: str, requester: str) -> str:
    """What a cell of the grid does, in the line the editor shows under the choice: the
    consequence for this pair, never a restatement of the choice. `claims` and `pressure`
    read differently from a workload because neither can be refused: a game starts with what
    is free and a short card simply stays short, so keeping loaded costs memory rather than a
    refusal. The Ask me lines say who decides, and they are what the runtime does: a claim and an
    episode run meanwhile, and a workload's request waits for the answer."""
    if c == "yes":
        return f"{holder} unloads once idle"
    if c == "ask":
        if requester_id == CLAIMS:
            return "you decide; the game runs meanwhile"
        if requester_id == PRESSURE:
            return "you decide what to free"
        return f"{requester} waits for your answer"
    if requester_id == CLAIMS:
        return "game runs with what's left"
    if requester_id == PRESSURE:
        return "nothing is freed"
    return f"{requester} is refused"


def matrix(rows, requesters) -> dict:
    """The grid: for each workload that can hold the card, and each thing that wants it, the
    one choice that pair holds and the sentence saying what it does.

    `rows` carry what the arbiter reads off its workloads - (name, label, yields_to,
    asks_for) - and `requesters` are (id, label) pairs: the other workloads, then `claims`
    and `pressure`. Those are the whole input on purpose: the page that edits this picks a
    choice per cell and never works out a rule of its own, so what it shows is what the next
    handover will do. A workload has no cell for itself: nothing unloads itself."""
    out = {}
    for name, label, yields_to, asks_for in rows:
        out[name] = {rid: {"choice": (c := choice(yields_to, asks_for, rid)),
                           "says": says(c, label, rid, rlabel)}
                     for rid, rlabel in requesters if rid != name}
    return out


def phrase(rid: str, label: str) -> str:
    """How a requester is named in the middle of a sentence: a workload by its label, a game and
    a short card by the two words that fit between other words."""
    return SUMMARY_PHRASE.get(rid, label)


def says_all(rows, requesters) -> dict:
    """Every cell of the grid with all three of its choices and the sentence each one carries.

    The cell on the page is a select, and the consequence line under it has to change the moment
    the owner picks a different answer - before anything is saved, and before this process has
    their draft at all. So the page is given the three sentences and shows one of them, rather
    than holding a sentence of its own for a choice it has never asked about."""
    return {name: {rid: {c: says(c, label, rid, rlabel) for c in CHOICES}
                   for rid, rlabel in requesters if rid != name}
            for name, label, _yields_to, _asks_for in rows}


def join_and(items) -> str:
    """A short list as it is read out: "a game", "ComfyUI and low VRAM", "a game, ComfyUI and low
    VRAM"."""
    if len(items) <= 1:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def row_says(rows, requesters) -> dict:
    """What a whole row of the grid says about one workload, as the sentence under it: "Strata
    makes room for a game and asks you first for ComfyUI."

    The page holds every choice and could build this itself; it doesn't, for the same reason
    `matrix` exists. A summary the page writes is a second account of the rule, and it is the
    half that goes stale when the reading of the two config keys changes. A row that makes room
    for everything says "anything" rather than the names, because that is what the file says and
    what a workload added next week will read too."""
    out = {}
    for name, label, yields_to, asks_for in rows:
        by = {c: [phrase(rid, rlabel) for rid, rlabel in requesters
                  if rid != name and choice(yields_to, asks_for, rid) == c] for c in CHOICES}
        if not by["ask"] and not by["keep"]:
            out[name] = f"{label} makes room for anything, including workloads you add later."
            continue
        parts = []
        if by["yes"]:
            parts.append("makes room for " + join_and(by["yes"]))
        if by["ask"]:
            parts.append("asks you first for " + join_and(by["ask"]))
        if by["keep"]:
            parts.append("stays loaded for " + join_and(by["keep"]))
        out[name] = f"{label} {join_and(parts)}."
    return out


def gib(mib) -> str:
    """MiB as the GiB the dashboard shows its owner: 21642 is "21.1 GiB". Said as a size the
    owner can spend, not as the number the driver returned."""
    return f"{mib / 1024:.1f} GiB"


def doing_phrase(label: str, working: bool, ago_s) -> str:
    """What a loaded workload is doing, as the sentence an outcome opens with: it is answering
    something now, it was used a moment ago so something may be mid-task, or it has been idle.

    Those are the three states the owner is weighing the consequence against, and they come from
    the two facts the release itself acts on - whether it is working, and when it was last seen
    working. No record is said as no record, the way `used_phrase` does it: a workload whose work
    was never stamped being called "idle" is exactly how an agent gets unloaded between steps."""
    if working:
        return f"{label} is answering a request right now. "
    if ago_s is None:
        return f"Nothing has recorded {label} working since this started, so it may be mid-task. "
    if ago_s < USED_RECENTLY_S:
        return f"{label} was {used_phrase(ago_s)}, so something may be mid-task. "
    return f"{label} has been {used_phrase(ago_s)}. "


def _cap(text: str) -> str:
    """A phrase at the start of a sentence: "the other app" reads "The other app"."""
    return text[0].upper() + text[1:]


# How each of the three answers plays out for each kind of requester, in the words the editor's
# "try it" shows before the owner has saved anything. `tone` is which of the three colours the
# page draws the box in: good for the answer that costs nothing they are in the middle of, warn
# for the one that interrupts or leaves them short, info for the one that stops and asks.
def outcome(*, holder: str | None, requester: str, requester_id: str, cell: str,
            working: bool, ago_s, held_mib, ask: dict, pressure_on: bool = True,
            loaded: bool = True) -> dict:
    """What one cell of the grid would do, as {title, detail, tone}.

    Built from the draft the page is holding plus the card's real state - is the workload working
    now, when was it last seen working, how much does it hold - because the demo of a rule has to
    be about the card as it is, not about a state the page made up to click through. It says
    nothing that the rule does not do: a kept row says who is refused, an ask says who waits and
    what happens when nobody answers, and a release says what comes free."""
    who = {CLAIMS: "the game", PRESSURE: "the other app"}.get(requester_id, requester)
    if requester_id == PRESSURE and not pressure_on:
        return {"tone": "info", "title": "Nothing is unloaded",
                "detail": "Low VRAM release is off."}
    if not loaded or not holder:
        return {"tone": "info", "title": "Nothing is loaded",
                "detail": "Nothing is on the card right now, so this would not unload anything."}
    if cell == "keep":
        detail = ("A game can't be refused, so it starts with what is free."
                  if requester_id == CLAIMS else
                  "Nothing is released. The other app has to manage with what is free."
                  if requester_id == PRESSURE else
                  f"{requester} is refused. The warning has a Make room now button, and the "
                  "refusal is logged.")
        return {"tone": "warn", "title": f"{holder} stays loaded, and you get a warning",
                "detail": detail}
    if cell == "ask":
        waits = ("The game runs with what is free until you answer." if requester_id == CLAIMS
                 else _cap(who) + " waits for your answer.")
        return {"tone": "info", "title": "You get a notification asking first",
                "detail": doing_phrase(holder, working, ago_s)
                          + "Pause your work if you need to, then choose Unload now or Keep it. "
                          + waits + " No answer in " + wait_phrase(ask.get("timeout_s")) + ": "
                          + (f"{holder} stays loaded." if ask.get("fallback") == KEEP
                             else f"{holder} unloads.")}
    if working:
        freed = f" and frees {gib(held_mib)} for {who}" if held_mib else ""
        return {"tone": "good", "title": f"{holder} finishes its current request, then unloads",
                "detail": "The request in progress is never cut off. As soon as it ends, "
                          f"{holder} unloads in about 2 s{freed}."}
    if ago_s is None or ago_s < USED_RECENTLY_S:
        return {"tone": "warn", "title": f"{holder} unloads at the next pause, even mid-task",
                "detail": doing_phrase(holder, working, ago_s)
                          + "An agent between two steps counts as idle, so " + holder
                          + " unloads now and its next step has to wait until the GPU is free "
                            "again. Choose Ask me if you'd rather decide."}
    return {"tone": "good", "title": f"{holder} unloads right away",
            "detail": doing_phrase(holder, working, ago_s)
                      + (f"Frees {gib(held_mib)} in about 2 s. It loads again the next time "
                         "something asks for it." if held_mib else
                         "It comes off in about 2 s and loads again the next time something "
                         "asks for it.")}

