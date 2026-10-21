"""What a claim does when it appears: free the card, without interrupting anything.

The rules themselves are tests/test_claims.py. This is the acting half and the reporting half:
`watch` (and the dashboard, which probes as often) noticing a claim with `on_start: release`,
unloading the idle workloads through their normal release path, leaving anything that is working
alone, recording what it did, retrying what is still holding while the claim lasts, and saying
all of that back in `claims` and `status`.

The claims here are real rules matched against a real (faked) process table, so the hits carry
their `on_start` the way they do on a machine with a game running.
"""

import json
import time
from pathlib import Path

from fakes import COMFY_CONTEXT_VRAM, COMFY_MODEL_VRAM, STRATA_VRAM
from vhotseat import claims, config as cfgmod, host, web

ROOT = Path(__file__).resolve().parents[1]

NAME, NOTE = "EldenRing.exe", "Elden Ring"


def claim_rule(rig, monkeypatch, on_start="release"):
    """Put a claim rule in force and the game in the process table. Returns the table: take the
    game out of it and the claim ends."""
    monkeypatch.setattr(rig.arb, "claim_rules",
                        claims.rules([{"name": NAME, "note": NOTE, "on_start": on_start}], builtin=False))
    monkeypatch.setattr(rig.arb, "claim_ignore", claims.ignores())
    procs = {4242: {"pid": 4242, "ppid": 4, "name": NAME, "exe": r"D:\Libraries\elden\EldenRing.exe"}}
    monkeypatch.setattr(host, "processes", lambda: procs)
    return procs


def rows(rig, kind):
    return [e for e in rig.events() if e["event"] == kind]


def running_render(rig, seconds=1.0):
    """A ComfyUI job on the fake's executor, with its weights on the card."""
    rig.comfy.slow_s = seconds
    rig.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})
    while not rig.comfy.running:
        time.sleep(0.01)


def test_a_claim_starting_unloads_the_idle_workloads_once(rig, monkeypatch):
    """The owner launches a game without closing anything first: Strata's model goes through its
    own unload, the server stays up, and it happens once - not once per poll."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    web.watch(rig.arb, polls=3)
    assert not rig.strata.loaded
    assert "POST /unload" in rig.strata.calls                # asked, never killed
    assert rig.host.active["strata"] is True                 # the service is still running
    out = rows(rig, "claim-release")
    assert len(out) == 1, out
    assert out[0]["workloads"] == ["strata"] and out[0]["freed_mib"] == STRATA_VRAM
    assert out[0]["freed"] == [{"workload": "strata", "freed_mib": STRATA_VRAM}]  # phase 3 reloads from this
    assert len(rows(rig, "claim")) == 1                      # one claim row for three polls


def test_the_claim_check_keeps_its_own_cadence(rig, monkeypatch):
    """A game has allocated within seconds of starting, so `watch` looks on `claims_poll_s`
    (2 s by default) whatever the telemetry sampling is set to. Here the sampler is slower than
    any wait: if the loop were still riding the telemetry interval, this would hang."""
    claim_rule(rig, monkeypatch)
    rig.arb.telemetry.interval_s = 600
    rig.arb.claim_poll_s = 0.01
    rig.strata.loaded = True
    web.watch(rig.arb, polls=2)                       # two claim checks, not two sample periods
    assert not rig.strata.loaded


def test_claims_poll_s_is_validated_and_reaches_the_arbiter(rig):
    """Default 2 s; the config's value is what the watcher uses, and a value that isn't a number
    of seconds is refused rather than silently disabling the watcher."""
    import pytest
    assert rig.arb.claim_poll_s == 2
    raw = cfgmod.parse((ROOT / "config.example.yaml").read_text())
    raw["claims_poll_s"] = 5
    assert cfgmod.validate(raw)["claims_poll_s"] == 5
    for bad in ("2", 0, -1, True):
        with pytest.raises(cfgmod.ConfigError, match="claims_poll_s"):
            cfgmod.validate({**raw, "claims_poll_s": bad})


def test_a_running_render_is_never_interrupted_and_goes_next(rig, monkeypatch):
    """Work in flight outranks a game: the render keeps its weights, the claim is recorded as
    waiting for it, and the moment it is idle - while the claim still holds - it is released."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    running_render(rig)
    web.watch(rig.arb, polls=1)
    assert not rig.strata.loaded                           # the idle one went
    assert rig.comfy.vram == COMFY_MODEL_VRAM              # the busy one did not
    waiting = rows(rig, "claim-release-waiting")
    assert len(waiting) == 1 and waiting[0]["workload"] == "comfy" and waiting[0]["why"]
    while rig.comfy.running or rig.comfy.pending:
        time.sleep(0.05)
    web.watch(rig.arb, polls=1)                            # a later poll, same claim
    assert rig.comfy.vram == COMFY_CONTEXT_VRAM
    assert [r["workloads"] for r in rows(rig, "claim-release")] == [["strata"], ["comfy"]]
    assert len(rows(rig, "claim")) == 1                    # still one claim, not a new one
    assert len(rows(rig, "claim-release-waiting")) == 1    # said once, then retried quietly


def test_a_claim_that_ends_first_leaves_the_work_alone(rig, monkeypatch):
    """The game quit before the render finished: there is no claim to free the card for any
    more, so nothing further is released and the record closes."""
    procs = claim_rule(rig, monkeypatch)
    running_render(rig)
    web.watch(rig.arb, polls=1)
    assert rows(rig, "claim-release-waiting")
    procs.clear()                                          # the game closed
    web.watch(rig.arb, polls=1)
    assert len(rows(rig, "claim-end")) == 1
    while rig.comfy.running or rig.comfy.pending:
        time.sleep(0.05)
    web.watch(rig.arb, polls=1)
    assert rig.comfy.vram == COMFY_MODEL_VRAM              # nobody came back for it
    assert rows(rig, "claim-release") == []


def test_on_start_block_is_the_old_behaviour(rig, monkeypatch):
    """`block`: refuse new loads, leave what is loaded alone. That is all claims used to do."""
    claim_rule(rig, monkeypatch, on_start="block")
    rig.strata.loaded = True
    web.watch(rig.arb, polls=2)
    assert rig.strata.loaded
    assert rows(rig, "claim-release") == []
    assert rows(rig, "claim-release-waiting") == []
    assert rows(rig, "claim")[0]["on_start"] == "block"


def test_a_release_never_runs_under_someone_elses_hold(rig, monkeypatch):
    """A `hold` owns the card and the work under it. Releasing underneath it would pull the
    ground out from under that work, so the watcher declines, names the holder, and asks again
    on the next poll."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    held = rig.arb.lock.try_take("hold comfy")
    with held:
        web.watch(rig.arb, polls=1)
    assert rig.strata.loaded                               # nothing released under the hold
    assert rows(rig, "claim-release") == []
    waiting = rows(rig, "claim-release-waiting")
    assert waiting and "hold comfy in progress" in waiting[0]["why"]
    web.watch(rig.arb, polls=1)                            # the hold is over: it goes
    assert not rig.strata.loaded


def test_two_pollers_free_the_card_once(rig, monkeypatch):
    """`web` and `watch` both run claim_transitions, both on their own schedule. One release and
    one set of rows: the log says what is already open and already released, and the GPU lock
    inside claim_release is what keeps the two from acting at the same moment."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    web.claim_transitions(rig.arb, rig.arb.claim_hits())     # the dashboard
    web.claim_transitions(rig.arb, rig.arb.claim_hits())     # watch
    assert not rig.strata.loaded
    assert len(rows(rig, "claim")) == 1
    assert len(rows(rig, "claim-release")) == 1
    assert rig.strata.calls.count("POST /unload") == 1


def test_claims_shows_the_action_and_what_it_has_done(rig, monkeypatch, capsys):
    """`vhotseat claims` answers three questions at once: which rule matched and what it does,
    what has been unloaded for this claim, and what is still holding and why."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    running_render(rig)
    web.watch(rig.arb, polls=1)
    assert rig.cli("claims") == 0
    out = capsys.readouterr().out
    assert "unloading what is loaded to free it" in out
    assert "-> on_start: release" in out
    assert f"for {NAME} ({NOTE}): unloaded strata (14000 MiB freed)" in out
    assert "comfy keeps its model:" in out


def test_claims_json_carries_the_action_too(rig, monkeypatch, capsys):
    claim_rule(rig, monkeypatch, on_start="block")
    assert rig.cli("claims", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hits"][0]["on_start"] == "block"
    assert out["rules"][0]["on_start"] == "block"


def test_status_shows_what_a_claim_was_given(rig, monkeypatch, capsys):
    """`vhotseat status` says not only that the card is claimed, but what was freed for the
    claim and what hasn't moved yet - the difference between a claim that worked and one that is
    waiting on a render."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    web.watch(rig.arb, polls=1)
    assert rig.cli("status") == 0
    out = capsys.readouterr().out
    assert "claims:" in out and "unloading what is loaded to free it" in out
    assert "freed for:" in out and f"unloaded strata ({STRATA_VRAM} MiB)" in out
