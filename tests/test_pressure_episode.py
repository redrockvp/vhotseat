"""The pressure episode: noticing an app vhotseat doesn't manage has taken the card, and answering
with the releases it can ask for.

The decision arithmetic is tests/test_pressure.py. This is the acting half: the three conditions
having to hold together, the episode being decided and recorded once, the release going through the
normal unload path, a busy workload waited for, a model the owner loads during an episode left
alone, and the whole thing being safe to run from two processes at once.

The scene in these tests is a 24576 MiB card with Strata's 14000 MiB model loaded, ComfyUI at its
400 MiB CUDA context, and an app nothing manages - blender.exe - holding the rest.
"""

import datetime
import json
import sqlite3
import time

from fakes import COMFY_MODEL_VRAM, STRATA_VRAM, FakeStrata
from vhotseat import host, pressure, util, web

GROWER = "blender.exe"

# The block as the tests want it: legible numbers rather than the measured defaults, which are
# sized for a card with 21 GiB of model on it.
NUMBERS = {"min_free_mib": 1500, "sustain_s": 20, "grow_mib": 2000, "headroom_mib": 500,
           "end_margin_mib": 3000, "cooldown_s": 600}


def rows(rig, kind):
    return [e for e in rig.events() if e["event"] == kind]


def samples(rig, *, free, other, n=8, every=5.0, ago=0.0):
    """The telemetry's history: `n` samples `every` seconds apart, the card under `free` MiB in
    every one of them and the unmanaged memory at `other`. The trigger reads its "has been short
    for 30 s" and its floor from here, not from anything it remembers."""
    for i in range(n):
        rig.arb.telemetry._write("INSERT INTO samples VALUES (?, ?, ?)",
                                 (time.time() - ago - i * every, "strata",
                                  json.dumps({"vram_free": free, "vram_other": other})))


def scene(rig, monkeypatch, *, grower_mib=9000, floor=1000, grower=GROWER, over=None):
    """A card short because of an app nothing manages, and nothing else that would answer it:
    Strata loaded, ComfyUI at its context, `grower` holding the rest, and the samples saying the
    card has been under the line for 35 s. Returns the grower's pid."""
    rig.arb.pressure_cfg.update({**NUMBERS, **(over or {})})
    rig.gpu.total = 24576
    rig.strata.loaded = True
    pid = rig.gpu.add_process(grower, grower_mib)
    rig.gpu.shared_of[pid] = 4000            # the counters' spill figure: evidence, never a trigger
    samples(rig, free=1176, other=floor)
    # The process table, because `pressure_over` asks whether the grower is still running and the
    # real one on whoever runs the suite has no blender in it.
    monkeypatch.setattr(host, "processes",
                        lambda: {pid: {"pid": pid, "ppid": 4, "name": grower, "exe": rf"C:\Render\{grower}"}})
    return pid


# --- the three conditions, together ----------------------------------------------


def test_a_card_short_for_long_enough_because_of_an_unmanaged_app_unloads_what_is_cheap(rig, monkeypatch):
    """All three at once: the card under the line, under it for longer than sustain_s, and the
    memory vhotseat can't manage far above its usual floor. The answer is the normal release path -
    API unload, then the card confirms the MiB went - and only the workload the config puts first."""
    scene(rig, monkeypatch)
    comfy_free = rig.comfy.calls.count("POST /free")
    out = rig.arb.pressure_start()
    assert out["declined"] is None
    assert out["released"] == [{"workload": "strata", "freed_mib": STRATA_VRAM}]
    assert not rig.strata.loaded and rig.strata.calls.count("POST /unload") == 1
    assert rig.comfy.vram == 400 and rig.comfy.calls.count("POST /free") == comfy_free
    assert rows(rig, "pressure-release") == [
        {"time": rows(rig, "pressure-release")[0]["time"], "event": "pressure-release",
         "source": "watch", "name": GROWER, "workloads": ["strata"],
         "freed": [{"workload": "strata", "mib": STRATA_VRAM}], "freed_mib": STRATA_VRAM}]


def test_the_episode_row_names_who_grew_by_how_much_over_how_long(rig, monkeypatch):
    """The one row that says why a model came off the card. A week later the owner asks "why did
    Strata lose its 14 GB?", and the answer has to be in the log: the process, what it holds, what
    the unmanaged memory usually is, how long the card was short, what the counters said about
    spill, and which workloads were chosen to pay for it."""
    pid = scene(rig, monkeypatch)
    assert rig.arb.pressure_start()["released"]
    ep = rows(rig, "pressure")[0]
    assert ep["name"] == GROWER and ep["pid"] == pid
    assert ep["other_mib"] == 9000 and ep["floor_mib"] == 1000 and ep["grew_mib"] == 8000
    assert ep["free_mib"] == 1176 and ep["min_free_mib"] == 1500
    assert 30 <= ep["low_for_s"] <= 40 and ep["samples"] == 8
    assert ep["need_mib"] == 1500 + 500 - ep["free_mib"]
    assert ep["targets"] == ["strata"] and ep["candidates"] == ["strata"]
    assert ep["shared_mib"] == 4000     # the card was oversubscribed: recorded, not acted on
    assert rig.arb.events.episode_open() is not None          # still short: it stays open

def test_a_single_dip_is_not_pressure(rig, monkeypatch):
    """One 5 s sample of a short card is not pressure: a tab opening, a video seeking, a workload
    that has just been asked to release. The low reading has to run across several samples, which is
    what `sustain_s` counts."""
    pid = scene(rig, monkeypatch)
    rig.arb.telemetry._db.execute("DELETE FROM samples")
    samples(rig, free=9000, other=1000, n=8, ago=5)       # comfortable for the last 40 s...
    samples(rig, free=1176, other=1000, n=1)              # ...and one low reading now
    out = rig.arb.pressure_start()
    assert "needs 20 s" in out["declined"]
    assert rig.strata.loaded and rows(rig, "pressure") == []
    assert pid


def test_our_own_models_filling_the_card_is_not_pressure(rig, monkeypatch):
    """The card being full is not the trigger: 21 GiB of our own models leaves nothing free and
    nobody was asked for anything. What has to have changed is the memory vhotseat can't manage -
    and the gate already handles our own growth."""
    scene(rig, monkeypatch, grower_mib=1000)              # the usual desktop, nothing new
    out = rig.arb.pressure_start()
    assert out["declined"] == ("the memory vhotseat doesn't manage is only 0 MiB over its usual "
                               "1000 MiB floor, needs 2000 MiB")
    assert rig.strata.loaded and rows(rig, "pressure") == []


def test_a_process_a_claim_already_covers_is_not_also_a_grower(rig, monkeypatch):
    """A game is phase 1's: its rule says what to do with it and it was already handed the card.
    Its memory is out of the growth measurement, so it doesn't also open an episode over the same
    gigabytes."""
    pid = scene(rig, monkeypatch)
    out = rig.arb.pressure_start(claimed={pid})
    assert out["declined"] == ("the memory vhotseat doesn't manage is only 0 MiB over its usual "
                               "1000 MiB floor, needs 2000 MiB")
    assert rig.strata.loaded


def test_a_workload_that_won_t_give_way_for_pressure_keeps_its_model(rig, monkeypatch):
    """`yields_to:` without `pressure` is the per-workload escape hatch: the owner has said this
    server doesn't lose its model for an app vhotseat doesn't manage. Then there is nobody to ask,
    and the honest answer is that nothing was released."""
    scene(rig, monkeypatch)
    rig.arb.workloads["strata"].yields_to = ("comfy", "claims")
    out = rig.arb.pressure_start()
    assert out["declined"] == "nothing of ours on the card will give way for pressure"
    assert rig.strata.loaded and rig.strata.calls.count("POST /unload") == 0


def test_pressure_off_means_nothing_is_ever_released_for_it(rig, monkeypatch):
    scene(rig, monkeypatch, over={"enabled": False})
    out = rig.arb.pressure_start()
    assert "pressure.enabled is false" in out["declined"]
    assert rig.strata.loaded and rows(rig, "pressure") == []


def test_a_process_the_owner_excluded_is_never_the_reason(rig, monkeypatch):
    """`ignore:` is the owner saying a VM, a benchmark or a miner may have the card. Its memory is
    out of the measurement, not just out of the sentence: nothing of ours comes back for it."""
    scene(rig, monkeypatch, grower="vmware-vmx.exe", over={"ignore": ["vmware-vmx.exe"]})
    out = rig.arb.pressure_start()
    assert out["declined"].endswith("needs 2000 MiB")
    assert rig.strata.loaded


def test_without_samples_there_is_no_such_thing_as_growth(rig, monkeypatch):
    """The trigger needs a sampler (watch, web or record) for both halves of its arithmetic: the
    floor to measure growth over and the run of low readings. Without them the answer is "I don't
    know", and not knowing doesn't unload a model."""
    scene(rig, monkeypatch)
    rig.arb.telemetry._db.execute("DELETE FROM samples")
    out = rig.arb.pressure_start()
    assert out["declined"].startswith("no samples to measure the growth against")
    assert rig.strata.loaded


def test_a_telemetry_that_cant_be_read_is_no_samples_not_a_broken_gate(rig, monkeypatch):
    """The samples are an optional feature, and every probe reads them: `status`, a switch and the
    Strata gate all go through the pressure readings. A locked or corrupt telemetry.db raised out of
    `floor_mib`, so one stuck database made `vhotseat status` traceback and refused to hand the card
    over. The answer is "I have no usable samples", which is the same decline as no sampler running,
    with the error named so the owner knows to fix the file."""
    scene(rig, monkeypatch)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    floor = rig.arb.telemetry.floor_mib
    monkeypatch.setattr(rig.arb.telemetry, "floor_mib", locked)
    assert rig.arb.pressure_start()["declined"] == (
        "no usable samples: the telemetry couldn't be read (database is locked)")
    why = rig.arb.probe()["pressure"]["why"]
    assert why.startswith("no usable samples") and "database is locked" in why
    assert rig.cli("switch", "comfy") == 0 and not rig.strata.loaded

    # The other read from the same file, the one only reached once the floor came back.
    monkeypatch.setattr(rig.arb.telemetry, "floor_mib", floor)
    monkeypatch.setattr(rig.arb.telemetry, "below_free", locked)
    assert "the telemetry couldn't be read" in rig.arb.pressure_readings()["why"]


def test_the_readings_explain_which_condition_is_missing(rig, monkeypatch):
    """`vhotseat why` and the dashboard quote these, so they have to say which of the three things
    isn't true rather than "no"."""
    scene(rig, monkeypatch)
    rig.arb.pressure_cfg["sustain_s"] = 3600                 # longer than any history the fakes write
    assert "needs 3600 s" in rig.arb.pressure_readings()["why"]
    samples(rig, free=9000, other=1000, n=2)                 # the card is fine now
    assert "hasn't been under 1500 MiB free" in rig.arb.pressure_readings()["why"]
    rig.gpu.error = "no counters"
    assert "couldn't be read" in rig.arb.pressure_readings()["why"]


# --- the release itself ----------------------------------------------------------


def test_a_busy_workload_is_left_alone_and_released_when_it_goes_idle(rig, monkeypatch):
    """Work in flight is never interrupted for an app nobody named. The target is recorded as
    waiting, the episode stays open while the claim lasts, and a later poll asks again: the same
    bounded patience phase 1 uses."""
    pid = scene(rig, monkeypatch, grower_mib=4000)
    rig.comfy.vram = COMFY_MODEL_VRAM
    job = rig.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})
    out = rig.arb.pressure_start()
    assert out["released"] == [] and out["declined"] is None
    assert out["waiting"][0]["workload"] == "comfy" and "queued" in out["waiting"][0]["why"] or \
        "running" in out["waiting"][0]["why"]
    assert rig.strata.loaded, "ComfyUI was first and Strata was never needed"
    waiting = rows(rig, "pressure-release-waiting")
    assert waiting[0]["attempts"] == 1 and waiting[0]["workload"] == "comfy"
    assert not rows(rig, "pressure-release")

    while rig.comfy.running or rig.comfy.pending:            # let the job finish
        time.sleep(0.05 * util.SCALE)
    ep = rig.arb.events.episode_open()
    assert ep["targets"] == ["comfy"] and ep["released"] == []
    out = rig.arb.pressure_release(ep)
    assert out["released"] == [{"workload": "comfy", "freed_mib": COMFY_MODEL_VRAM - 1500}]
    assert rig.comfy.vram == 400 and rig.strata.loaded
    assert rows(rig, "pressure-release")[0]["workloads"] == ["comfy"]
    assert rig.arb.events.episode_open()["released"] == ["comfy"]
    assert pid and job


def test_a_workload_that_says_it_unloaded_and_keeps_holding_is_counted(rig, monkeypatch):
    """The card, not the server's reply, decides. /free answers and ComfyUI keeps its models - an
    extension that doesn't give the memory back - so it isn't recorded as released, and the reason
    says what the card still shows."""
    scene(rig, monkeypatch, grower_mib=4000)
    rig.comfy.vram = COMFY_MODEL_VRAM
    rig.comfy.stuck_vram = COMFY_MODEL_VRAM
    rig.arb.workloads["comfy"].priority = 10                # ask the cheap one first
    out = rig.arb.pressure_start()
    assert out["released"] == [] and out["waiting"][0]["workload"] == "comfy"
    assert "kept 7000 MiB" in out["waiting"][0]["why"]
    assert rig.arb.events.episode_open()["released"] == []
    assert rig.strata.loaded, "the release that didn't happen paid for nothing"


def test_a_second_actor_finds_the_card_already_clear_and_writes_nothing(rig, monkeypatch):
    """`watch` and the dashboard both run this. The log says an episode is open, so the second one
    doesn't start another; and inside the GPU lock the card is already what the first left it, so it
    unloads nothing again and writes no second row."""
    scene(rig, monkeypatch)
    first = rig.arb.pressure_start()
    assert first["released"]
    again = rig.arb.pressure_start()
    assert again["declined"] == "a pressure episode is already open"
    ep = rig.arb.events.episode_open()
    second = rig.arb.pressure_release(ep)
    assert second["released"] == [] and second["freed_mib"] == 0
    assert rig.strata.calls.count("POST /unload") == 1
    assert len(rows(rig, "pressure")) == 1 and len(rows(rig, "pressure-release")) == 1


def test_a_card_being_short_doesn_t_walk_the_lock_when_nothing_is_owed(rig, monkeypatch):
    """Once every workload the episode was owed is free, later polls return before the GPU lock and
    the card read: a box with an episode open for hours must not take and release the lock every
    couple of seconds for work it has already done."""
    scene(rig, monkeypatch)
    rig.arb.pressure_start()

    def boom(label):
        raise AssertionError(f"the GPU lock was taken for {label}")

    monkeypatch.setattr(rig.arb.lock, "try_take", boom)
    ep = rig.arb.events.episode_open()
    assert rig.arb.pressure_release(ep) == {"declined": None, "by_actor": False, "would": [],
                                           "released": [], "waiting": [], "freed_mib": 0}


def test_a_model_the_owner_loads_during_an_episode_is_left_alone(rig, monkeypatch):
    """The workloads an episode is owed were chosen when it started, from what the card held then.
    A switch the owner types in the middle of it is an order, and the next poll does not take it
    back: a trigger that re-read the card every poll would fight the user all day. Phase 1's bug,
    learned once already."""
    scene(rig, monkeypatch, over={"end_margin_mib": 20000})   # the room never quite comes back
    out = rig.arb.pressure_start()
    assert out["released"] and not rig.strata.loaded
    rig.strata.loaded = True                                # the owner put it back
    ep = rig.arb.events.episode_open()
    assert rig.arb.pressure_release(ep)["released"] == []
    assert rig.strata.loaded and rig.strata.calls.count("POST /unload") == 1


def test_the_episode_ends_when_the_room_is_back_above_the_wider_margin(rig, monkeypatch):
    """Ending at the line that started it would restart the episode the moment the app allocated
    again, and unloading and reloading 14 GB over and over is exactly what this must not do. The end
    asks for more room than the trigger asked for."""
    scene(rig, monkeypatch)
    rig.arb.pressure_start()
    ep = rig.arb.events.episode_open()
    assert rig.arb.pressure_over(ep) == "the card is back to 15176 MiB free"


def test_the_episode_ends_when_the_grower_is_gone(rig, monkeypatch):
    """The app took the card and then closed: whatever room is left, there is nobody left to make
    room for, and an episode that outlives its reason would keep a later grower waiting behind its
    cooldown."""
    pid = scene(rig, monkeypatch, over={"end_margin_mib": 20000})
    rig.arb.pressure_start()
    ep = rig.arb.events.episode_open()
    monkeypatch.setattr(host, "processes", lambda: {})
    assert rig.arb.pressure_over(ep) == f"{GROWER} is gone"
    assert pid


def test_no_second_episode_inside_the_cooldown(rig, monkeypatch):
    """The last episode's own rows decide when the next may start: the memory released has to be
    reloaded before the card is in the state another release would fix, and a wrong firing costs
    that reload either way."""
    scene(rig, monkeypatch)
    rig.arb.pressure_start()
    rig.arb.events.record("pressure-end", source="watch", name=GROWER, reason="the card is back")
    again = rig.arb.pressure_start()
    assert "the last pressure episode was" in again["declined"] and "cooldown is 600 s" in again["declined"]
    assert rig.strata.calls.count("POST /unload") == 1


def test_a_cooldown_that_has_passed_starts_a_fresh_episode(rig, monkeypatch):
    """The cooldown is a wait, not a life sentence: once it has passed, an app still holding the
    card gets a fresh episode with a fresh target set."""
    scene(rig, monkeypatch)
    rig.arb.pressure_start()
    rig.arb.events.record("pressure-end", source="watch", name=GROWER, reason="the card is back")
    log = rig.state / "events.jsonl"
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    old = (datetime.datetime.now().astimezone() - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
    for e in lines:
        if e["event"] in pressure.EPISODE_ROWS:
            e["time"] = old
    log.write_text("\n".join(json.dumps(e) for e in lines) + "\n", encoding="utf-8")
    rig.strata.loaded = True
    out = rig.arb.pressure_start()
    assert out["declined"] is None and rig.strata.calls.count("POST /unload") == 2


# --- the two actors that run it --------------------------------------------------


def test_both_pollers_together_open_one_episode_and_unload_once(rig, monkeypatch):
    """The dashboard's watcher and `vhotseat watch` both run this, on their own clocks. One
    episode in the log and one unload: the log says what is already open and already given, and the
    GPU lock is what keeps the two from acting in the same moment."""
    scene(rig, monkeypatch)
    web.watch_pass(rig.arb, [])                            # the dashboard
    web.watch_pass(rig.arb, [])                            # watch
    assert not rig.strata.loaded
    assert len(rows(rig, "pressure")) == 1 and len(rows(rig, "pressure-release")) == 1
    assert rig.strata.calls.count("POST /unload") == 1


def test_the_watcher_that_runs_at_logon_runs_the_pressure_pass_too(rig, monkeypatch):
    """`vhotseat watch` is the resident process autostart installs, and the trigger's readings need
    a sampler, which is the same process. One poll of it is enough to open the episode."""
    scene(rig, monkeypatch)
    web.watch(rig.arb, polls=1)
    assert not rig.strata.loaded and len(rows(rig, "pressure")) == 1


def test_a_stuck_workload_is_asked_three_times_and_then_left(rig, monkeypatch):
    """A workload that answers "unloaded" and keeps its 14000 MiB has an unload that doesn't work,
    not a busy moment. Asking it again every poll for as long as the app holds the card spends the
    whole time taking the GPU lock for nothing, so the attempts in the rows are counted - from the
    rows, so both pollers count the same three - and then it is left alone for the episode."""
    scene(rig, monkeypatch)
    # An unload that doesn't work: /unload answers "unloaded", the confirm the owner wrote agrees,
    # and the card still says the 14000 MiB is there. The card decides.
    monkeypatch.setattr(FakeStrata, "vram", property(lambda self: STRATA_VRAM))
    rig.arb.workloads["strata"].spec.setdefault("confirm", {})["vram_at_most"] = STRATA_VRAM
    for _ in range(web.STUCK_TRIES):
        web.pressure_pass(rig.arb)
    got = rows(rig, "pressure-release-waiting")
    assert [r["attempts"] for r in got] == [1, 2, 3]
    assert all(r["stuck"] and r["held_mib"] == STRATA_VRAM for r in got)
    unloads = rig.strata.calls.count("POST /unload")
    web.pressure_pass(rig.arb)
    assert rig.strata.calls.count("POST /unload") == unloads == web.STUCK_TRIES
    assert rig.arb.events.episode_open() is not None, "the episode stays open: the card is short"


def test_the_episode_is_closed_in_the_log_when_the_room_comes_back(rig, monkeypatch):
    """The close has to be a row too: it is what tells `why`, the dashboard and the cooldown that
    this episode is over, rather than a fact one process happens to remember."""
    scene(rig, monkeypatch)
    web.pressure_pass(rig.arb)
    assert not rig.strata.loaded
    web.pressure_pass(rig.arb)                             # the room came back: close it
    assert [e["reason"] for e in rows(rig, "pressure-end")] == ["the card is back to 15176 MiB free"]
    assert rig.arb.events.episode_open() is None


# --- what it says about itself ---------------------------------------------------


def test_a_status_probe_carries_the_episode_it_is_acting_on(rig, monkeypatch):
    """Mid-flight: an owner looking at the dashboard while this happens must be able to see which
    app took the card, how far past its usual it is, and what has come off so far. Every number
    that made the decision is in the same probe that shows the decision, from the same card read -
    a report that read the card again to explain itself could disagree with what was acted on."""
    scene(rig, monkeypatch, grower="blender.exe", grower_mib=9000)
    web.pressure_pass(rig.arb)
    pr = rig.arb.probe()["pressure"]
    assert pr["would"] is False, "the room came back, so it isn't pressure now"
    assert pr["why"] == "the card is back above 1500 MiB free" or pr["why"] == "nothing unmanaged grew"
    ep = pr["open"]["episode"]
    assert ep["name"] == "blender.exe" and ep["grew_mib"] == 8000 and ep["other_mib"] == 9000
    assert ep["free_mib"] == 1176 and ep["floor_mib"] == 1000 and ep["min_free_mib"] == 1500
    assert pr["open"]["released"] == ["strata"] and pr["open"]["freed_mib"] == STRATA_VRAM
    assert pr["last"]["name"] == "blender.exe" and pr["last"]["event"] == "pressure-release"
    assert pr["last"]["time"] == ep["time"], "while it runs, the newest row is this episode's"
    assert pr["last"]["freed"] == [{"workload": "strata", "mib": STRATA_VRAM}]


def test_the_status_text_says_what_took_the_card_and_what_came_off(rig, monkeypatch):
    """`vhotseat status` is what an owner types when the models vanish. One line, in the words the
    release wrote: the app, how far past its usual it sits, and what has come off for it."""
    scene(rig, monkeypatch, grower="blender.exe", grower_mib=9000)
    web.pressure_pass(rig.arb)
    out = rig.arb.describe(rig.arb.probe())
    line = [l for l in out.splitlines() if l.startswith("pressure:")][0]
    assert "blender.exe holds 9000 MiB vhotseat doesn't manage" in line
    assert "8000 MiB over its usual 1000" in line
    assert "card at 1176 MiB free for" in line
    assert "unloaded Strata (14000 MiB)" in line


def test_why_names_the_episode_that_unloaded_a_workload_and_says_it_stays_off(rig, monkeypatch):
    """After the fact, in `vhotseat why`: the question an owner asks ten minutes after a game or a
    render closes is whether the vanished model was unloaded for that app and whether it will come
    back on its own. It won't - only loading something, or `switch`, is the order to refill - and
    the note says who grew and by how much."""
    scene(rig, monkeypatch, grower="blender.exe", grower_mib=9000)
    web.pressure_pass(rig.arb)
    why = rig.arb.why_all()["strata"]
    joined = " | ".join(why["notes"])
    assert "came off for the pressure episode (blender.exe grew to 8000 MiB)" in joined
    assert "won't chase it back on" in joined
    assert "nothing loads it back while the card is short this way" in joined


def test_why_says_which_workload_the_episode_is_still_owed(rig, monkeypatch):
    """Mid-flight, the other half: a workload that has not come off yet is not stuck, it is owed,
    and the note says so rather than leaving the owner to read the event log by hand."""
    scene(rig, monkeypatch, grower="blender.exe", grower_mib=9000)
    rig.comfy.vram = COMFY_MODEL_VRAM
    rig.arb.workloads["strata"].yields_to = ("comfy",)      # not a pressure target: kept out
    rig.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})   # a render in flight
    web.pressure_pass(rig.arb)
    ep = rig.arb.events.episode_open()
    assert ep["targets"] == ["comfy"] and ep["released"] == []
    assert [w["workload"] for w in ep["waiting"]] == ["comfy"] and not ep["waiting"][0]["stuck"]
    joined = " | ".join(rig.arb.why_all()["comfy"]["notes"])
    assert "still owed this workload: it has not come off yet" in joined
    joined = " | ".join(rig.arb.why_all()["strata"]["notes"])
    assert "doesn't cover pressure" in joined


def test_a_closed_episode_is_still_explained_after_it_ends(rig, monkeypatch):
    """An hour later, `open` is empty and `last` carries the whole thing: who grew, what was asked,
    what came free, and the reason it closed. That is what tells an owner the unload was this
    feature's and not a crash, and it is a row rather than a memory, so the process that started
    the episode and the one reading it an hour later both have it."""
    scene(rig, monkeypatch, grower="blender.exe", grower_mib=9000)
    web.pressure_pass(rig.arb)
    web.pressure_pass(rig.arb)                              # the room came back: it closes
    pr = rig.arb.probe()["pressure"]
    assert pr["open"] is None
    assert pr["last"]["event"] == "pressure-end"
    assert pr["last"]["name"] == "blender.exe" and pr["last"]["freed_mib"] == STRATA_VRAM
    assert pr["last"]["reason"] == "the card is back to 15176 MiB free"
    joined = " | ".join(rig.arb.why_all()["strata"]["notes"])
    assert "the pressure episode for blender.exe ended" in joined
