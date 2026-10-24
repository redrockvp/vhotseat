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


def yields(spec, reason: str) -> bool:
    """May a workload with this `yields_to:` be unloaded for `reason` - a workload's
    name, `claims` or `pressure`?"""
    return ANYONE in spec or reason in spec


def describe(spec) -> str:
    """What a workload gives way for, as the words a status line, a refusal or the
    dashboard use: "anything", "nothing", or the list with the special words spelled
    out."""
    if ANYONE in spec:
        return "anything"
    if not spec:
        return "nothing"
    return ", ".join(PHRASE.get(s, s) for s in spec)
