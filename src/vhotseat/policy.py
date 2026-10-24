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
rather than a rule with two readings. Until the asking itself exists, an `ask` is exactly a
keep at runtime: nothing unloads for that newcomer, and `status` and `why` say so rather
than letting the owner believe an answer is on its way.

`ask:` (top level) is what an unanswered ask is worth: `timeout_s` to wait, and `fallback`
of `keep` or `release` for what to do when nobody answers. Nothing waits yet; the numbers
are recorded so the asking has something to be written against.

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
DEFAULT_YIELDS_TO = ("*",)
# `asks_for:` has no `*`: "ask me before unloading me for anyone" is not a question anyone
# can answer, and an absent key means it asks for nobody, which is the old behaviour.
SPECIAL_ASK = (CLAIMS, PRESSURE)
DEFAULT_ASKS_FOR = ()

# What an unanswered ask is worth, until the asking exists (`ask:`).
ASK_DEFAULTS = {"timeout_s": 120, "fallback": "keep"}
FALLBACKS = ("keep", "release")

# How the special words read in a sentence shown to a person.
PHRASE = {CLAIMS: "a claim on the card", PRESSURE: "the card running short"}

# The two requesters that aren't a workload, as the editor shows them: what a column is
# called, and the line under it that says who that is.
SPECIAL_REQUESTERS = ((CLAIMS, "A game", "or any app that claims the GPU"),
                      (PRESSURE, "Low VRAM", "another app squeezes the card"))


def ask_cfg(raw: dict | None) -> dict:
    """The `ask:` block with the defaults filled in, as a plain dict the way the other
    blocks are, so the dashboard can show it and the asking can be written against it."""
    raw = raw or {}
    return {k: raw.get(k, v) for k, v in ASK_DEFAULTS.items()}


def yields(spec, reason: str, asks_for=()) -> bool:
    """May a workload with this `yields_to:` and `asks_for:` be unloaded for `reason` - a
    workload's name, `claims` or `pressure`? Only a `yes` may: asking first keeps it loaded,
    which is what it will do until the asking exists and what it does today either way."""
    return choice(spec, asks_for, reason) == "yes"


def choice(yields_to, asks_for, reason: str) -> str:
    """What one pair holds: "yes" (make room), "ask" (ask me first) or "keep" (neither).

    One function because the grid, the config and the arbiter must not each decide which of
    the two keys wins. A name in both is a config error, so the only overlap left is `"*"`
    in `yields_to:` facing a named ask, and there the narrower answer is the one that counts.
    `ask` is not consent to unload: everything that asks "may this come off?" calls `yields`,
    which answers no for it. That is deliberate, and the only place part A lets the asking
    exist without the asking being built: a cell set to Ask me keeps its workload loaded, so
    an owner can write the grid the design shows and nothing unloads that shouldn't."""
    if reason in asks_for:
        return "ask"
    return "yes" if ANYONE in yields_to or reason in yields_to else "keep"


def describe(spec, asks_for=()) -> str:
    """What a workload gives way for, as the words a status line, a refusal or the
    dashboard use: "anything", "nothing", or the list with the special words spelled
    out - and, when it wants asking first, said as what it is today rather than left to
    read as a question someone will answer."""
    out = ("anything" if ANYONE in spec else "nothing" if not spec else names(spec))
    return f"{out}, and asks {names(asks_for)} first ({NOT_YET})" if asks_for else out


def names(spec) -> str:
    """A list of policy entries as a person reads it: "a claim on the card", "comfy"."""
    return ", ".join(PHRASE.get(s, s) for s in spec)


# What an `ask` is worth until part B builds the notification, in every sentence that
# mentions one. An owner who sets Ask me and reads a status line must not believe an answer
# is on its way: the key is here, the consequence isn't.
NOT_YET = "asking isn't built yet, so it stays loaded"


def why_not(yields_to, asks_for, reason: str, who: str) -> str:
    """Why a workload is not being unloaded for `reason`, as the clause in the middle of the
    sentence the refusal, the kept row and `why` all say: the key to change and what it says
    about this particular newcomer. `who` is how that sentence names the newcomer, because
    "ComfyUI" and "a claim on the card" are different readings of the same entry."""
    if choice(yields_to, asks_for, reason) == "ask":
        return f"its asks_for asks {who} first, and {NOT_YET}"
    return f"its yields_to ({describe(yields_to)}) doesn't cover {who}"


def says(c: str, holder: str, requester_id: str, requester: str) -> str:
    """What a cell of the grid does, in the line the editor shows under the choice: the
    consequence for this pair, never a restatement of the choice. `claims` and `pressure`
    read differently from a workload because neither can be refused: a game starts with what
    is free and a short card simply stays short, so keeping loaded costs memory rather than a
    refusal. The Ask me lines say who decides; the arbiter's own sentences (`describe`,
    `why_not`) add what isn't built yet."""
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

