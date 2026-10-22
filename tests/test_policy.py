"""Who may ask a workload to let go of its models: the two config keys, what the arbiter
does with them at every point where it decides to unload something, and what the reports
and the dashboard say about it afterwards.

The rules themselves are policy.py: `yields_to:` says which newcomers a workload will make
room for, `priority:` the order among the ones that will. Unloading is one thing and
loading another: a load nobody chose can be refused, a `switch` you typed is an order, and
freeing the card for a claim is never refused by either.
"""

import pytest
from pathlib import Path

from fakes import COMFY_MODEL_VRAM
from vhotseat import api, config, policy, setup

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
