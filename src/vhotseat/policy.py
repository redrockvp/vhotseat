"""Who may ask a workload to let go of its models, and in what order.

Unloading used to be one rule for everything: whatever held memory on the card was a
candidate for every handover. `yields_to:` makes it a per-workload decision - for each
loaded workload, which newcomers may unload it and which are refused instead. An entry
is another workload's name, `claims` (a process that owns the card by right, see
claims.py), `pressure` (the card running short because of a process vhotseat doesn't
manage), or `*` for anyone. The default when the key is absent is `["*"]`, which is
exactly the old behaviour, and an empty list means it gives way for nobody: only a
typed `vhotseat switch` moves it.

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

# How the special words read in a sentence shown to a person.
PHRASE = {CLAIMS: "a claim on the card", PRESSURE: "the card running short"}


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
