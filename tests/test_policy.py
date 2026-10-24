"""Who may ask a workload to let go of its models: the two config keys, what the arbiter
does with them at every point where it decides to unload something, and what the reports
and the dashboard say about it afterwards.

The rules themselves are policy.py: `yields_to:` says which newcomers a workload will make
room for, `priority:` the order among the ones that will. Unloading is one thing and
loading another: a load nobody chose can be refused, a `switch` you typed is an order, and
freeing the card for a claim is never refused by either.
"""

import json
from pathlib import Path

import pytest

from fakes import COMFY_CONTEXT_VRAM, COMFY_MODEL_VRAM, STRATA_VRAM
from vhotseat import api, claims, config, host, policy, setup, web

ROOT = Path(__file__).resolve().parents[1]


def edited(**policy_keys):
    """config.example.yaml with the policy keys set per workload: the key is a workload name
    and the value the keys to write under it, as in edited(strata={"priority": 20})."""
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    for name, keys in policy_keys.items():
        raw["workloads"][name].update(keys)
    return config.validate(raw)


# --- the config keys ---------------------------------------------------------------


def test_a_config_that_says_nothing_about_policy_behaves_as_it_always_did(rig):
    """Default `["*"]` is every handover there has ever been: a config written before this
    existed behaves exactly as it did, and the values the arbiter reads say so."""
    for name in ("strata", "comfy"):
        w = rig.arb.workloads[name]
        assert w.yields_to == ("*",) and w.priority == 50
        assert policy.describe(w.yields_to) == "anything"


def test_what_is_written_reaches_the_workload(rig):
    arb = rig.host.install(setup.build(edited(strata={"yields_to": ["comfy", "claims"],
                                                                "priority": 20}), gpu=rig.gpu))
    assert arb.workloads["strata"].yields_to == ("comfy", "claims")
    assert arb.workloads["strata"].priority == 20
    assert arb.workloads["comfy"].yields_to == ("*",)


def test_a_policy_that_refuses_everyone_is_written_as_an_empty_list(rig):
    """`yields_to: []` is a real answer - nobody may unload it, not even a game - and must
    not be defaulted back to "anyone" on the way in."""
    arb = rig.host.install(setup.build(edited(strata={"yields_to": []}), gpu=rig.gpu))
    assert arb.workloads["strata"].yields_to == ()
    assert not policy.yields(arb.workloads["strata"].yields_to, "comfy")
    assert not policy.yields(arb.workloads["strata"].yields_to, "claims")


def test_yields_to_names_workloads_or_the_three_words(rig):
    """A name that isn't a workload in this config is a mistake in the config, not a rule
    that quietly never matches: it says which newcomer was meant, and a typo means the
    owner believes they have a rule they don't."""
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    raw["workloads"]["strata"]["yields_to"] = ["strat"]
    with pytest.raises(config.ConfigError, match=r"workloads\.strata\.yields_to\[0\]"):
        config.validate(raw)
    for good in (["comfy"], ["claims"], ["pressure"], ["*"], ["comfy", "claims"], []):
        raw["workloads"]["strata"]["yields_to"] = good
        config.validate(raw)
    for bad in (["comfy", "strataa"], "comfy", ["*  "], [None]):
        raw["workloads"]["strata"]["yields_to"] = bad
        with pytest.raises(config.ConfigError, match="yields_to"):
            config.validate(raw)


def test_priority_is_a_whole_number_from_0_to_100(rig):
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    for good in (0, 50, 100):
        raw["workloads"]["strata"]["priority"] = good
        assert config.validate(raw)["workloads"]["strata"]["priority"] == good
    for bad in (-1, 101, "50", 5.5, True):
        raw["workloads"]["strata"]["priority"] = bad
        with pytest.raises(config.ConfigError, match=r"workloads\.strata\.priority"):
            config.validate(raw)


def test_a_workload_isnt_called_claims_or_pressure():
    """Those two are what `yields_to:` means by them. A workload named `claims` would make
    one written rule read one way and act another."""
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    raw["workloads"]["claims"] = raw["workloads"]["comfy"]
    with pytest.raises(config.ConfigError, match="a workload name must be"):
        config.validate(raw)
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    raw["workloads"]["pressure"] = raw["workloads"]["comfy"]
    with pytest.raises(config.ConfigError, match="a workload name must be"):
        config.validate(raw)


# --- refusing a load nobody chose ----------------------------------------------------


def give_way(rig, **per):
    """What each workload will make room for, on the live arbiter: strata=["claims"] means
    only a game (or a command typed by the owner) moves Strata."""
    for name, spec in per.items():
        rig.arb.workloads[name].yields_to = tuple(spec)


def rows(rig, kind):
    return [e for e in rig.events() if e["event"] == kind]


def test_the_default_policy_refuses_nothing(rig):
    """With nothing written, both workloads hold the card, and a guard's requested switch
    still gets the card by unloading the other: the old one-rule-for-everything behaviour,
    kept on purpose."""
    rig.strata.loaded = True
    rig.comfy.vram = COMFY_MODEL_VRAM
    assert rig.cli("switch", "comfy", "--request") == 0
    assert not rig.strata.loaded
    assert rows(rig, "policy-refused") == []


def test_a_load_nobody_chose_is_refused_when_the_card_wont_give_way(rig, capsys):
    """ComfyUI's guard asks on the queue's behalf. If Strata won't give way for ComfyUI, the
    job is refused rather than Strata unloaded over: unloading 20 GB the owner chose to keep
    is not what a queue gets to decide. The refusal says who is in the way and what it does
    give way for, which is the key to change."""
    give_way(rig, strata=["claims"])
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy", "--request") == 1
    assert rig.strata.loaded                                    # asked, never unloaded
    assert "POST /unload" not in rig.strata.calls
    got = rows(rig, "policy-refused")
    assert len(got) == 1 and got[0]["requested"] == "comfy" and got[0]["workloads"] == ["strata"]
    assert got[0]["ok"] is False
    assert "Strata holds 14000 MiB" in got[0]["why"] and "a claim on the card" in got[0]["why"]
    assert "refused" in capsys.readouterr().err


def test_the_llms_own_gate_is_refused_the_same_way(rig):
    """The other direction: ComfyUI keeps its models for a game only, so Strata's before_load
    hook is refused while ComfyUI holds weights, and the answer the agent sees is the same
    503 any other refusal gives."""
    give_way(rig, comfy=["claims"])
    rig.comfy.vram = COMFY_MODEL_VRAM
    assert rig.strata.request() == 503
    assert not rig.strata.loaded
    got = rows(rig, "policy-refused")
    assert got and got[-1]["source"] == "gate" and got[-1]["requested"] == "strata"
    assert got[-1]["workloads"] == ["comfy"]
    load = [e for e in rows(rig, "strata-load") if e["allowed"] is False][-1]
    assert "ComfyUI holds" in load["reason"]


def test_a_workload_holding_nothing_refuses_nobody_whatever_its_policy_says(rig):
    """Strata gives way for nobody, and its model isn't on the card: there is nothing here
    for ComfyUI to be short of, so ComfyUI loads. A policy read off the config rather than
    the card would block the card with an idle server, which is the fastest way to make
    people delete the config."""
    give_way(rig, strata=[])
    assert rig.strata.loaded is False
    assert rig.cli("switch", "comfy", "--request") == 0


def test_a_switch_you_type_unloads_anyway_and_says_whose_policy_it_overrode(rig):
    """The same state as the refusal above, with a person deciding: a typed `switch` is an
    order everywhere else in vhotseat and is one here too. It logs the override rather than
    pretending nothing unusual happened, so "why did Strata lose its model an hour ago?"
    has an answer in the log."""
    give_way(rig, strata=[])
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy") == 0
    assert not rig.strata.loaded                                # unloaded, by order
    got = rows(rig, "policy-override")
    assert len(got) == 1 and got[0]["requested"] == "comfy" and got[0]["workloads"] == ["strata"]
    assert "yields_to" in got[0]["why"]
    assert rows(rig, "policy-refused") == []


def test_the_workload_that_gives_way_first_is_the_one_with_less_priority(rig):
    """Both hold the card and both may be unloaded: the one with the lower priority is asked
    first, and within a tie the config's order still decides, which is what it was before
    there were priorities."""
    order = []
    for name, at in (("strata", 90), ("comfy", 10)):
        w = rig.arb.workloads[name]
        w.priority = at
        real = w.release
        w.release = lambda wait, timeout, _r=real, _n=name: (order.append(_n), _r(wait, timeout))[1]
    rig.strata.loaded = True
    rig.comfy.vram = COMFY_MODEL_VRAM
    assert rig.cli("switch", "none") == 0
    assert order == ["comfy", "strata"]


# --- what a claim may and may not unload ---------------------------------------------

NAME, NOTE = "EldenRing.exe", "Elden Ring"


def claim_rule(rig, monkeypatch):
    """A claim rule in force and the game in the process table, as in tests/test_claim_release.py:
    here the question is which workloads the claim is allowed to unload."""
    rig.arb.claim_rules = claims.rules([{"name": NAME, "note": NOTE}], builtin=False)
    rig.arb.claim_ignore = claims.ignores()
    monkeypatch.setattr(host, "processes", lambda: {
        4242: {"pid": 4242, "ppid": 4, "name": NAME, "exe": r"D:\Libraries\elden\EldenRing.exe"}})


def test_a_claim_leaves_a_workload_that_doesnt_give_way_for_games_alone(rig, monkeypatch, capsys):
    """Strata gives way for ComfyUI only. A game starting doesn't unload it, and doesn't
    pretend to be waiting for it either: it was never owed. ComfyUI goes as always, and the
    log says who is still holding what, because the game may be short of exactly that much."""
    claim_rule(rig, monkeypatch)
    give_way(rig, strata=["comfy"])
    rig.strata.loaded = True
    rig.comfy.vram = COMFY_MODEL_VRAM
    web.watch(rig.arb, polls=1)
    assert rig.strata.loaded                                   # kept, not unloaded
    assert rig.comfy.vram == COMFY_CONTEXT_VRAM                # and this one went
    assert [r["workloads"] for r in rows(rig, "claim-release")] == [["comfy"]]
    targets = rows(rig, "claim-release-targets")[0]
    assert targets["targets"] == ["comfy"] and targets["kept"] == ["strata"]
    kept = rows(rig, "policy-kept")
    assert len(kept) == 1 and kept[0]["workload"] == "strata"
    assert kept[0]["held_mib"] == STRATA_VRAM
    assert "doesn't cover a claim on the card" in kept[0]["why"]
    assert rows(rig, "claim-release-waiting") == []            # waiting is for what is owed


def test_a_long_game_writes_the_kept_warning_once(rig, monkeypatch):
    """The release for a claim is re-checked on every poll for as long as the game runs, so a
    warning written per poll would bury the log in the same line every 2 s - and the dashboard
    banner decides what is news from the row's age, not from a count."""
    claim_rule(rig, monkeypatch)
    give_way(rig, strata=["comfy"])
    rig.strata.loaded = True
    web.watch(rig.arb, polls=4)
    assert len(rows(rig, "policy-kept")) == 1
    assert rig.strata.loaded


def test_a_workload_that_refuses_claims_is_never_owed_to_one(rig, monkeypatch):
    """Even a target list that names it - written before the owner protected it, or by hand -
    doesn't make it a candidate: the list is what the card held when the claim started, this is
    what the config says now."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    rig.arb.workloads["strata"].yields_to = ()
    out = rig.arb.claim_release({"name": NAME, "note": NOTE}, only=["strata"])
    assert out["released"] == [] and out["freed_mib"] == 0
    assert rig.strata.loaded
    assert "POST /unload" not in rig.strata.calls


def test_a_claim_asks_the_lowest_priority_to_give_way_first(rig, monkeypatch):
    """Which workload loses its model first, among the ones that may be unloaded at all:
    priority, not the config's order. With both at the default 50 the order is unchanged."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    rig.comfy.vram = COMFY_MODEL_VRAM
    assert rig.arb.claim_targets() == ["comfy", "strata"]
    rig.arb.workloads["strata"].priority = 10
    rig.arb.workloads["comfy"].priority = 90
    assert rig.arb.claim_targets() == ["strata", "comfy"]


def test_status_and_claims_say_what_a_claim_was_not_given(rig, monkeypatch, capsys):
    """`the card is claimed, and ComfyUI went` is only half the answer when the game is still
    14 GB short: the other half is that Strata was never asked, and what to change if the owner
    wants it asked next time."""
    claim_rule(rig, monkeypatch)
    give_way(rig, strata=["comfy"])
    rig.strata.loaded = True
    rig.comfy.vram = COMFY_MODEL_VRAM
    web.watch(rig.arb, polls=1)
    assert rig.cli("claims") == 0
    out = capsys.readouterr().out
    assert f"for {NAME} ({NOTE}): unloaded comfy" in out
    assert "kept for its policy: strata" in out
    assert rig.cli("status") == 0
    assert "kept for its policy: strata" in capsys.readouterr().out


# --- asking first -------------------------------------------------------------------


def test_asks_for_takes_the_same_names_as_yields_to_less_the_star(rig):
    """The third answer takes the same vocabulary as the other two, so a row of the grid can
    be written by hand as easily as it is clicked. Not the `"*"`: "ask me before unloading me
    for anyone" is a question no owner can answer, and it would swallow the row."""
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    for good in (["comfy"], ["claims"], ["pressure"], ["comfy", "pressure"], []):
        raw["workloads"]["strata"]["asks_for"] = good
        config.validate(raw)
    for bad in (["*"], ["strat"], "comfy", [None]):
        raw["workloads"]["strata"]["asks_for"] = bad
        with pytest.raises(config.ConfigError, match="asks_for"):
            config.validate(raw)


def test_an_asks_for_left_empty_is_the_message_doctor_prints(rig):
    """`asks_for:` written with nothing after it - or with `null` - is the mistake an owner makes
    meaning to write the list. It has to be answered the way an empty `yields_to:` is answered: the
    key named, what a list of them looks like. A present key that skips validation instead reaches
    `tuple(None)` where the workloads are built, and then nothing reads the file: a fresh command
    dies on a traceback, and a `watch` that has been up all day exits on the poll after the save."""
    example = (ROOT / "config.example.yaml").read_text()
    for written in ("asks_for:", "asks_for: null", "asks_for:   # I'll write it after lunch"):
        text = example.replace("    allow_restart: true", f"    {written}", 1)
        with pytest.raises(config.ConfigError, match="asks_for: expected a list"):
            config.parse(text)


def test_a_workload_isnt_named_in_both_lists(rig):
    """`yields_to: [comfy]` next to `asks_for: [comfy]` says it makes room for ComfyUI and
    that it waits for an answer first. One of the two has to be what it means, so the config
    says so rather than picking a winner by key order."""
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    raw["workloads"]["strata"]["yields_to"] = ["comfy", "claims"]
    raw["workloads"]["strata"]["asks_for"] = ["claims"]
    with pytest.raises(config.ConfigError, match="both yields_to: and asks_for:"):
        config.validate(raw)
    # `"*"` covers everyone, so it isn't naming the same newcomer twice; the ask is the
    # narrower answer and the matrix reads it as the one that counts.
    raw["workloads"]["strata"]["yields_to"] = ["*"]
    config.validate(raw)


def test_the_ask_block_is_a_wait_and_what_happens_with_no_answer(rig):
    """Owner-side numbers, same style as `pressure:`: a wait and a fallback. Nothing waits
    yet, so a typo here would be a typo in a feature the owner can't see working."""
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    for good in ({"timeout_s": 120}, {"fallback": "release"},
                 {"timeout_s": 5.5, "fallback": "keep"}):
        raw["ask"] = good
        assert config.validate(raw)["ask"] == good
    for bad, why in (({"fallback": "ask"}, "ask.fallback"), ({"timeout_s": 0}, "ask.timeout_s"),
                     ({"timeout_s": "2 min"}, "ask.timeout_s"), ({"ask_me": True}, "unknown key"),
                     (["keep"], "expected a mapping")):
        raw["ask"] = bad
        with pytest.raises(config.ConfigError, match=why):
            config.validate(raw)


def test_the_ask_defaults_are_recorded_not_invented_by_the_reader(rig):
    """One place says what an unanswered ask is worth, and the config's absence of a key means
    that number rather than a second default somewhere else."""
    assert policy.ask_cfg(None) == {"timeout_s": 120, "fallback": "keep"}
    assert policy.ask_cfg({"timeout_s": 30, "fallback": "release"}) == {
        "timeout_s": 30, "fallback": "release"}
    assert policy.ask_cfg({"fallback": "release"})["timeout_s"] == 120


# --- what an ask decides today ------------------------------------------------------


def ask_first(rig, **per):
    """Who wants asking first before it is unloaded, on the live arbiter: strata=["comfy"]
    means a ComfyUI handover asks Strata first. Same shape as `give_way`."""
    for name, spec in per.items():
        rig.arb.workloads[name].asks_for = tuple(spec)


def test_an_ask_keeps_its_workload_loaded_for_a_load_nobody_chose(rig, capsys):
    """The asking isn't built, and that has to be a no-op rather than a surprise: Strata wants
    to be asked before ComfyUI unloads it, so the guard's request is refused exactly as a keep
    is - nothing partially unloads, because the card cannot be given to ComfyUI anyway. The
    sentence says which of the two it is, so the owner isn't left waiting for a notification
    that will never arrive."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy", "--request") == 1
    assert rig.strata.loaded
    assert "POST /unload" not in rig.strata.calls
    got = rows(rig, "policy-refused")
    assert len(got) == 1 and got[0]["workloads"] == ["strata"]
    assert "its asks_for asks ComfyUI first" in got[0]["why"]
    assert "asking isn't built yet, so it stays loaded" in got[0]["why"]


def test_a_workload_that_wants_asking_first_still_holds_for_a_game(rig, monkeypatch):
    """A claim is never refused, so an ask in a game's row is the same as a keep: the game
    starts short of whatever stayed put, and `policy-kept` is where that is recorded. Not
    waiting: nothing is waiting for an answer, and a target list that named it anyway is not
    asked either."""
    claim_rule(rig, monkeypatch)
    ask_first(rig, strata=["claims"])
    rig.strata.loaded = True
    rig.comfy.vram = COMFY_MODEL_VRAM
    web.watch(rig.arb, polls=1)
    assert rig.strata.loaded
    assert [r["workloads"] for r in rows(rig, "claim-release")] == [["comfy"]]
    assert rows(rig, "claim-release-targets")[0]["kept"] == ["strata"]
    kept = rows(rig, "policy-kept")
    assert len(kept) == 1 and kept[0]["workload"] == "strata"
    assert "its asks_for asks a claim on the card first" in kept[0]["why"]
    assert rig.arb.claim_release({"name": NAME, "note": NOTE}, only=["strata"])["released"] == []


def test_a_switch_you_type_moves_a_workload_that_wants_asking_first(rig):
    """An ask is not a second veto: a `switch` you typed is an order here exactly as it is for
    a keep, and says whose policy it overrode. What is missing is the asking, not the owner's
    ability to decide in person."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy") == 0
    assert not rig.strata.loaded
    assert [r["requested"] for r in rows(rig, "policy-override")] == ["comfy"]


def test_no_workload_is_owed_to_a_pressure_episode_it_wants_asked_for(rig):
    """Same reasoning as a claim: pressure releases what may be released and refuses nothing,
    so an ask takes that workload out of the eviction set rather than opening a question."""
    ask_first(rig, strata=["pressure"])
    rig.strata.loaded = True
    rig.comfy.vram = COMFY_MODEL_VRAM
    assert [n for n, _ in rig.arb.pressure_candidates()] == ["comfy"]


def test_status_and_why_say_which_answer_an_ask_is_today(rig, monkeypatch, capsys):
    """Both the owner's readings of the policy - `status` and `why` - name the key and say what
    it does now, because "it gives way for anything, and asks a claim on the card first" read
    next to a game running with the rest of the card is the difference between a mystery and a
    config line to change."""
    claim_rule(rig, monkeypatch)
    ask_first(rig, strata=["claims"])
    rig.strata.loaded = True
    assert rig.cli("status") == 0
    out = capsys.readouterr().out
    assert "strata 50 (anything, and asks a claim on the card first " \
           "(asking isn't built yet, so it stays loaded))" in out
    assert rig.cli("why", "comfy") == 0
    out = capsys.readouterr().out
    assert "keeps its model for the claim: its asks_for asks a claim on the card first" in out
    assert rig.cli("why", "--json") == 0
    assert json.loads(capsys.readouterr().out)["strata"]["asks_for"] == ["claims"]


# --- the grid the editor writes -----------------------------------------------------

ROWS = [("strata", "Strata", ("*",), ()),
        ("comfy", "ComfyUI", ("strata",), ()),
        ("game", "Game", ("comfy",), ("claims", "pressure", "strata"))]
REQUESTERS = [("strata", "Strata"), ("comfy", "ComfyUI"), ("game", "Game"),
              ("claims", "A game"), ("pressure", "Low VRAM")]


def test_a_grid_cell_holds_the_choice_and_what_it_does():
    """The page shows a choice and a consequence per cell; both come from the two keys the
    arbiter reads, so what an owner sees before saving is what the next handover does, and JS
    never has a rule of its own to keep in sync."""
    grid = policy.matrix(ROWS, REQUESTERS)
    assert grid["strata"]["comfy"] == {"choice": "yes", "says": "Strata unloads once idle"}
    assert grid["strata"]["claims"]["choice"] == "yes"
    # The diagonal: nothing unloads itself.
    assert "comfy" not in grid["comfy"]
    keep = grid["comfy"]
    assert keep["game"]["choice"] == "keep"
    assert keep["game"]["says"] == "Game is refused"
    assert keep["claims"]["says"] == "game runs with what's left"
    assert keep["pressure"]["says"] == "nothing is freed"
    ask = grid["game"]
    assert ask["claims"]["choice"] == "ask"
    assert ask["claims"]["says"] == "you decide; the game runs meanwhile"
    assert ask["pressure"]["says"] == "you decide what to free"
    assert ask["strata"]["says"] == "Strata waits for your answer"
    assert ask["comfy"] == {"choice": "yes", "says": "Game unloads once idle"}


def test_the_grid_and_the_arbiter_read_the_same_cells():
    """One vocabulary, used by both: a cell the page paints Keep is a cell the handover
    refuses, and the `"*"`/ask overlap resolves the same way in both, because it's the same
    function answering."""
    for yields_to, asks_for in ((("*",), ()), (("comfy",), ()), (("comfy",), ("claims",)),
                                (("*",), ("claims",)), ((), ()), ((), ("pressure",))):
        for reason in ("comfy", "claims", "pressure", "strata"):
            c = policy.choice(yields_to, asks_for, reason)
            assert policy.yields(yields_to, reason, asks_for) == (c == "yes")


# --- saying it back -----------------------------------------------------------------


def test_status_prints_who_gives_way_for_whom(rig, capsys):
    """The two keys decide what gets unloaded, so they belong next to the memory they decide
    the fate of: an owner reading `status` mid-fight shouldn't have to open the config to find
    out which line to change."""
    give_way(rig, strata=["comfy"])
    rig.arb.workloads["comfy"].priority = 30
    assert rig.cli("status") == 0
    out = capsys.readouterr().out
    assert "strata 50 (comfy)" in out and "comfy 30 (anything)" in out
    assert rig.cli("status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["policy"]["strata"] == {
        "priority": 50, "yields_to": ["comfy"], "gives_way_for": "comfy"}


def test_why_calls_a_workload_that_wont_give_way_a_blocker_not_a_delay(rig, capsys):
    """`why` is the dry run of the handover, so it has to be as strict as the handover: a
    workload that refuses is `blocked`, not `would have to wait`, and the note names the key
    that would stop refusing."""
    give_way(rig, strata=["claims"])
    rig.strata.loaded = True
    assert rig.cli("why", "comfy") == 0
    out = capsys.readouterr().out
    assert "is blocked" in out and "yields_to (a claim on the card)" in out
    assert "Strata holds 14000 MiB and won't give way for ComfyUI" in out
    assert "would be asked to release it" not in out
    assert rig.cli("why", "--json") == 0
    assert json.loads(capsys.readouterr().out)["comfy"]["would"] == "blocked"


def test_why_names_a_workload_a_claim_wont_unload(rig, monkeypatch, capsys):
    """A game is short of VRAM and 14 GB is still sitting in Strata because of a key in the
    config: `why` is where that gets explained, on the workload the owner is asking about."""
    claim_rule(rig, monkeypatch)
    give_way(rig, strata=["comfy"])
    rig.strata.loaded = True
    assert rig.cli("why", "comfy") == 0
    assert "Strata keeps its model for the claim" in capsys.readouterr().out
