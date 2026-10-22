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

from vhotseat import config, policy, setup

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
