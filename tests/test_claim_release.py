"""What a claim does when it appears: free the card, without interrupting anything.

The rules themselves are tests/test_claims.py. This is the acting half and the reporting half:
`watch` (and the dashboard, which probes as often) noticing a claim with `on_start: release`,
unloading the idle workloads through their normal release path, leaving anything that is working
alone, recording what it did, retrying what is still holding while the claim lasts, and saying
all of that back in `claims` and `status`.

The claims here are real rules matched against a real (faked) process table, so the hits carry
their `on_start` the way they do on a machine with a game running.
"""

import contextlib
import json
import re
import threading
import time
from pathlib import Path

from fakes import COMFY_CONTEXT_VRAM, COMFY_MODEL_VRAM, STRATA_VRAM, FakeStrata
from vhotseat import claims, config as cfgmod, host, setup, web

ROOT = Path(__file__).resolve().parents[1]

NAME, NOTE = "EldenRing.exe", "Elden Ring"


def claim_rule(rig, monkeypatch, on_start="release", arb=None):
    """Put a claim rule in force and the game in the process table. Returns the table: take the
    game out of it and the claim ends. Pass `arb` to arm a second arbiter over the same log,
    which is what a restarted `watch` - or the dashboard alongside it - is."""
    arb = arb or rig.arb
    monkeypatch.setattr(arb, "claim_rules",
                        claims.rules([{"name": NAME, "note": NOTE, "on_start": on_start}], builtin=False))
    monkeypatch.setattr(arb, "claim_ignore", claims.ignores())
    procs = {4242: {"pid": 4242, "ppid": 4, "name": NAME, "exe": r"D:\Libraries\elden\EldenRing.exe"}}
    monkeypatch.setattr(host, "processes", lambda: procs)
    return procs


@contextlib.contextmanager
def dashboard(rig, monkeypatch):
    """The real dashboard on a port of its own: `web.serve`, which is what autostart runs and the
    only thing that starts the claim watcher thread. It is shut down and joined on the way out.
    Left running, it outlives its test and goes on probing whatever process table the next test
    has put in place - the suite used to end with `claim check failed: AttributeError: FakeK32
    object has no attribute OpenProcess`."""
    seen = {}
    real = web.ThreadingHTTPServer

    class Tracked(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen["srv"] = self

    monkeypatch.setattr(web, "ThreadingHTTPServer", Tracked)
    th = threading.Thread(target=web.serve, args=(rig.arb, "127.0.0.1", 0), daemon=True)
    th.start()
    while "srv" not in seen:
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{seen['srv'].server_address[1]}"
    finally:
        seen["srv"].shutdown()             # serve_forever returns, and its finally stops the watcher
        th.join(timeout=10)


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



def test_the_dashboard_frees_the_card_with_nobody_looking(rig, monkeypatch):
    """On a box where autostart runs `vhotseat web` and not `watch`, the dashboard is the actor -
    and it has to notice the game whether or not somebody has the page open."""
    claim_rule(rig, monkeypatch)
    rig.arb.claim_poll_s = 0.02
    rig.strata.loaded = True
    with dashboard(rig, monkeypatch):
        deadline = time.monotonic() + 10
        while rig.strata.loaded and time.monotonic() < deadline:
            time.sleep(0.05)
    assert not rig.strata.loaded
    assert rows(rig, "claim-release")
    # The thread it started is gone rather than polling the next test's process table. It wakes
    # on its own interval, so give it that much time rather than assuming it noticed already.
    left = lambda: [t for t in threading.enumerate() if t.name == "vhotseat-claims"]  # noqa: E731
    deadline = time.monotonic() + 5
    while left() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert left() == []


def test_two_watchers_colliding_say_nothing_and_try_again(rig, monkeypatch):
    """watch and the dashboard can both be mid-check. The one that loses the lock doesn't report
    every workload as waiting on the other watcher: that fills the log with a reason nobody asked
    about, and - reported once per claim - it shadows the reason that matters when it turns up."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    with rig.arb.lock.try_take(f"claim release {NAME}"):        # the other actor, mid-release
        web.claim_transitions(rig.arb, rig.arb.claim_hits())
        assert rig.strata.loaded
        assert rows(rig, "claim-release") == []
        assert rows(rig, "claim-release-waiting") == []         # no noise about the other actor
    web.watch(rig.arb, polls=1)                                 # and it simply tries again
    assert not rig.strata.loaded

def test_a_poll_that_raises_does_not_end_the_watcher(rig, monkeypatch, capsys):
    """The dashboard's background check has swallowed a bad poll for a long time; `vhotseat watch`
    - the process `autostart` keeps running, and on most boxes the only actor - had no such guard.
    One pass tripping over a driver, an event log that moved out from under it, or a process that
    vanished mid-scan ended the watcher, and nothing released the card for the rest of the day.
    It says what happened and carries on."""
    real = web.watch_pass
    seen = []

    def flaky(arbiter, hits):
        seen.append(hits)
        if len(seen) == 1:
            raise OSError("the process scan fell over")
        real(arbiter, hits)

    monkeypatch.setattr(web, "watch_pass", flaky)
    assert web.watch(rig.arb, polls=2) == 0
    assert len(seen) == 2, "the loop stopped at the poll that raised"
    assert "the process scan fell over" in capsys.readouterr().err


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



def test_the_actor_behind_the_lock_finds_the_card_already_clear(rig, monkeypatch):
    """watch and the dashboard can be mid-check at the same moment. The lock serialises them,
    and the one that gets in second re-reads the card inside the lock: it frees nothing and
    writes nothing, instead of unloading a second time and writing a second row."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    first = rig.arb.claim_release({"name": NAME, "note": NOTE})
    assert first["released"] == [{"workload": "strata", "freed_mib": STRATA_VRAM}]
    second = rig.arb.claim_release({"name": NAME, "note": NOTE})
    assert second["released"] == [] and second["freed_mib"] == 0 and second["declined"] is None
    assert rig.strata.calls.count("POST /unload") == 1


def test_one_bad_poll_does_not_stop_the_watcher(rig, monkeypatch):
    """`watch` starts at logon and stays up for weeks. A release that blows up - a driver that
    raises where it should have refused - is logged and tried again, not fatal."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True

    def explode(claim):
        raise RuntimeError("driver bug")

    monkeypatch.setattr(rig.arb, "claim_release", explode)
    assert web.watch(rig.arb, polls=2) == 0
    assert rows(rig, "claim")
    assert rig.strata.loaded                                   # nothing released, nothing crashed

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


def test_a_status_label_is_never_glued_to_what_it_labels(rig, monkeypatch, capsys):
    """The column is as wide as the longest workload name, and `freed for:` is longer than
    most of them: with short names the label ran into the process behind it -
    `freed for:EldenRing.exe` - on the one line that says what the claim was given."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    web.watch(rig.arb, polls=1)
    assert rig.cli("status") == 0
    out = capsys.readouterr().out
    assert re.search(r"freed for:\s+\S", out), out


def test_a_model_loaded_during_a_claim_is_left_alone(rig, monkeypatch):
    """The owner is mid-game and types `vhotseat switch strata`. A typed switch is an order, so
    the watcher does not fight it - and it must not undo it either, which is what reading the
    card fresh every 2 s would do. The release was decided from what the card held when the
    game started; what the owner chooses afterwards is theirs."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    running_render(rig)                              # comfy holds weights and is working
    web.watch(rig.arb, polls=1)
    assert not rig.strata.loaded
    assert rows(rig, "claim-release-targets")[0]["targets"] == ["comfy", "strata"]
    rig.strata.loaded = True                         # the typed switch, mid-game
    while rig.comfy.running or rig.comfy.pending:
        time.sleep(0.05)
    web.watch(rig.arb, polls=3)
    assert rig.comfy.vram == COMFY_CONTEXT_VRAM      # the render's weights went once it went idle
    assert rig.strata.loaded                         # nobody came back for the new load
    assert rig.strata.calls.count("POST /unload") == 1
    assert [r["workloads"] for r in rows(rig, "claim-release")] == [["strata"], ["comfy"]]


def test_a_workload_holding_nothing_is_not_a_target(rig, monkeypatch):
    """ComfyUI is up with only its CUDA context on the card: the game is short of VRAM, not of
    400 MiB, so it isn't on the list of what the claim is owed. Once strata has gone the claim
    has been given its due, and the watcher stops asking entirely rather than taking the GPU
    lock and reading the card every 2 s for as long as the game runs."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    web.watch(rig.arb, polls=1)
    assert rows(rig, "claim-release-targets")[0]["targets"] == ["strata"]
    asked = []
    monkeypatch.setattr(rig.arb, "claim_release", lambda *a, **k: asked.append(1) or {})
    web.watch(rig.arb, polls=3)
    assert asked == []
    assert rig.strata.loaded is False


def test_a_restarted_watcher_honours_the_recorded_targets(rig, monkeypatch):
    """`watch` restarting - the logon autostart does that at every reboot, games and all - reads
    the claim, the list it was owed, and what has gone, back from the event log instead of
    deciding again from the card. A model loaded after the game started is still nobody's to
    unload, and the claim isn't written a second time."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    web.watch(rig.arb, polls=1)
    assert not rig.strata.loaded
    rig.comfy.vram = COMFY_MODEL_VRAM                # the owner went to comfy mid-game
    fresh = rig.host.install(setup.build(rig.cfg, gpu=rig.gpu))   # a new process, same log
    claim_rule(rig, monkeypatch, arb=fresh)
    web.watch(fresh, polls=2)
    assert rig.comfy.vram == COMFY_MODEL_VRAM
    assert len(rows(rig, "claim")) == 1
    assert [r["workloads"] for r in rows(rig, "claim-release")] == [["strata"]]


def test_an_unload_that_leaves_its_vram_behind_is_tried_three_times(rig, monkeypatch):
    """Its release answers "unloaded" and the card still shows the weights: an owner's `confirm:`
    more generous than `idle_vram_mib`, or a node that doesn't hand memory back. It is not
    recorded as released - `claim-release` says "unloaded" and must only name what the card
    agrees is clear - and it is retried, bounded: asking forever would spend the whole game
    taking the GPU lock for nothing. The last row says it gave up."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    # The lie: the fake keeps claiming its weights after a successful /unload, and its own
    # confirm is set so the release itself considers itself done.
    monkeypatch.setattr(FakeStrata, "vram", property(lambda self: STRATA_VRAM))
    rig.arb.workloads["strata"].spec.setdefault("confirm", {})["vram_at_most"] = STRATA_VRAM
    rig.arb.claim_poll_s = 0.01
    web.watch(rig.arb, polls=8)
    assert rows(rig, "claim-release") == []
    got = rows(rig, "claim-release-waiting")
    assert [r["attempts"] for r in got] == [1, 2, 3]
    assert all(r["stuck"] and r["held_mib"] == STRATA_VRAM for r in got)
    assert "still holds 14000 MiB" in got[0]["why"]
    assert "leaving it until the claim ends" in got[-1]["why"]
    assert rig.strata.calls.count("POST /unload") == 3      # asked three times, then stopped


def test_a_retry_that_works_clears_the_row_saying_it_was_stuck(rig, monkeypatch, capsys):
    """The second try gets the memory back: it is recorded as released, and `status` stops
    saying it is still holding. An owner reading `vhotseat status` mid-game should see what the
    claim was given now, not every problem the release ran into on the way."""
    claim_rule(rig, monkeypatch)
    rig.strata.loaded = True
    lying = [True]
    monkeypatch.setattr(FakeStrata, "vram", property(lambda self: STRATA_VRAM if lying[0] else 0))
    rig.arb.workloads["strata"].spec.setdefault("confirm", {})["vram_at_most"] = STRATA_VRAM
    rig.arb.claim_poll_s = 0.01
    web.watch(rig.arb, polls=1)
    assert rows(rig, "claim-release") == []
    assert [r["attempts"] for r in rows(rig, "claim-release-waiting")] == [1]
    lying[0] = False
    web.watch(rig.arb, polls=1)
    assert [r["workloads"] for r in rows(rig, "claim-release")] == [["strata"]]
    assert rig.cli("status") == 0
    out = capsys.readouterr().out
    assert f"unloaded strata ({STRATA_VRAM} MiB)" in out
    assert "keeps its model" not in out
