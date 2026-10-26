"""What happens when a workload loads past a gate that never asks.

`gate: {kind: hold}` - Unsloth Studio on the owner's box - means there is nothing inside the program
that asks vhotseat for the GPU before a load: no hook, no plugin node, no proxy. So a model loaded
from Studio's own UI, or by a chat request that wakes it, asks nobody, makes no room, and is seen by
nobody either: `watch_pass` handles claims and pressure, and pressure only counts memory vhotseat
doesn't manage. The card ends up with two residents and a model that sized itself, at launch, to
whatever was free.

The owner chose the fix of acting after the fact, and these tests are that rule: who counts as an
arrival, what each kind of Rules-grid cell does about it, what gets logged and what doesn't, that two
watchers on one box do it once, and that the whole thing ends when the conflict does.
"""

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fakes import COMFY_CONTEXT_VRAM, COMFY_MODEL_VRAM, STRATA_VRAM, FakeComfy, FakeGpu, FakeHost, FakeStrata

from vhotseat import config as cfgmod
from vhotseat import setup, web

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def arig(tmp_path, monkeypatch):
    """Strata, ComfyUI and a third workload with nothing inside it that could ask: `kind: hold`.
    The same fakes as the rest of the suite, with one more server on a port of its own - and no
    `on_load` hook on it, because that is what a hold gate means."""
    strata, comfy, studio, h = FakeStrata(), FakeComfy(), FakeStrata(), FakeHost()
    h.active["studio"] = True
    h.mem["studio"] = {"ram_mib": 100, "swap_mib": 0, "file_mib": 0}
    gpu = FakeGpu(strata, comfy)
    gpu.more["studio"] = studio
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    raw = cfgmod.parse((ROOT / "config.example.yaml").read_text())
    raw["workloads"]["strata"]["url"] = strata.url
    raw["workloads"]["comfy"]["url"] = comfy.url
    raw["claims"], raw["claims_builtin"] = [], False
    raw["workloads"]["studio"] = {
        "driver": "http", "label": "Unsloth Studio", "url": studio.url,
        "service": {"systemd-user": "studio"},
        "resident": {"get": "/health", "path": "loaded"},
        "release": {"post": "/unload?wait={wait}"},
        "confirm": {"vram_at_most": 0},
        "gate": {"kind": "hold"},
    }
    cfg = cfgmod.validate(raw)
    arb = h.install(setup.build(cfg, gpu=gpu))

    class Rig:
        pass

    r = Rig()
    r.strata, r.comfy, r.studio, r.host, r.gpu = strata, comfy, studio, h, gpu
    r.arb, r.cfg, r.arb2 = arb, cfg, None
    r.state = tmp_path / "state" / "vhotseat"

    def events(kind=None):
        path = r.state / "events.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        return rows if kind is None else [e for e in rows if e["event"] == kind]

    r.events = events
    yield r
    for srv in (strata, comfy, studio):
        srv.close()


def twin(rig):
    """A second watcher over the same log and the same card: a second `watch`, or the dashboard
    running beside one. The point of building it here is that it remembers nothing the first one
    saw, which is what the event log and the GPU lock are for."""
    rig.arb2 = rig.host.install(setup.build(rig.cfg, gpu=rig.gpu))
    return rig.arb2


def check(arb, n=1):
    """The watcher's pass, without the telemetry sampler or the wait between polls."""
    for _ in range(n):
        web.watch_pass(arb, arb.claim_hits())


def asks_first(rig, name="comfy"):
    """The Ask me cell for that workload's studio column, on every watcher the test has running: two
    watchers are two arbiters, each built from the config, and the rule lives on the objects."""
    for arb in (rig.arb, rig.arb2):
        if arb is not None:
            arb.workloads[name].asks_for = ("studio",)


def running_render(rig, seconds=1.0):
    """A ComfyUI job on the fake's executor, with its weights on the card."""
    rig.comfy.slow_s = seconds
    rig.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})
    while not rig.comfy.running:
        time.sleep(0.01)


def loaded(rig):
    """ComfyUI has models resident and is idle, then a model appears in Unsloth Studio: nothing was
    asked, nothing was freed, and the card now has two residents."""
    rig.comfy.vram = COMFY_MODEL_VRAM
    check(rig.arb)                       # the pass that records what was already on the card
    rig.studio.loaded = True             # Studio's own UI: no gate, no hook, no ask
    rig.studio.loading = False


def test_an_arrival_unloads_the_idle_workload_that_owes_it(arig):
    """ComfyUI is holding models and doing nothing when Unsloth loads: the grid says comfy makes room
    for studio, so its models go through its own release path, the newcomer is left running with what
    it took, and it happens once for the arrival rather than once per poll."""
    loaded(arig)
    check(arig.arb)
    assert arig.comfy.vram == COMFY_CONTEXT_VRAM      # its models are gone
    assert "POST /free" in arig.comfy.calls           # asked, never killed
    assert arig.host.active["comfyui"] is True        # the service is still running
    assert arig.studio.loaded is True                 # the load itself is never undone
    one = arig.events("arrival")
    assert len(one) == 1 and one[0]["arriving"] == "studio"
    assert one[0]["others"] == ["comfy"]              # what it loaded on top of
    targets = arig.events("arrival-release-targets")
    assert len(targets) == 1 and targets[0]["targets"] == ["comfy"]
    rel = arig.events("arrival-release")
    assert len(rel) == 1, rel
    assert rel[0]["workloads"] == ["comfy"]
    assert rel[0]["freed_mib"] == COMFY_MODEL_VRAM - 1500     # above its idle weight
    check(arig.arb)                                   # the conflict is over: one workload left
    end = arig.events("arrival-end")
    assert len(end) == 1 and end[0]["arriving"] == "studio"


def test_the_release_says_what_the_owner_has_to_do_next(arig):
    """The memory came free only after the model was loaded, so that model is holding less of the card
    than it could: a row that says "comfy unloaded" leaves the owner with a free card and the same
    small model. The release row carries the sentence that says to load it again."""
    loaded(arig)
    check(arig.arb)
    note = arig.events("arrival-release")[0]["note"]
    assert note == ("Unsloth Studio loaded while ComfyUI was on the card, so it sized itself to what "
                    f"was free. Reload the model in Unsloth Studio to use the "
                    f"{COMFY_MODEL_VRAM - 1500} MiB freed.")


def test_a_workload_that_is_working_is_never_interrupted(arig):
    """A render in flight keeps the card, whatever loaded beside it: comfy is reported as waiting
    rather than released, and it goes on holding until the job ends. Then a later pass releases it,
    because the conflict - and the room the newcomer needs - is still there."""
    loaded(arig)
    running_render(arig, seconds=1.5)
    check(arig.arb)
    assert arig.comfy.vram == COMFY_MODEL_VRAM               # not touched
    wait = arig.events("arrival-release-waiting")
    assert len(wait) == 1 and wait[0]["workload"] == "comfy"
    assert "busy" in wait[0]["why"] or "running" in wait[0]["why"]
    assert not arig.events("arrival-release")
    while arig.comfy.running:
        time.sleep(0.05)
    check(arig.arb)
    assert arig.comfy.vram == COMFY_CONTEXT_VRAM
    assert len(arig.events("arrival-release")) == 1          # one unload, not two


def test_keep_loaded_holds_its_model_and_says_who_may_be_short(arig):
    """The cell for Unsloth Studio says Keep loaded: nothing is released, and the arrival goes ahead
    with what is free. The one record is a `policy-kept` row naming the workload, how much it holds
    and what the config says, because the honest summary is that the load may now be short of exactly
    that much."""
    arig.arb.workloads["comfy"].yields_to = ("strata", "claims", "pressure")
    loaded(arig)
    check(arig.arb)
    assert arig.comfy.vram == COMFY_MODEL_VRAM               # left holding
    assert not arig.events("arrival-release")
    targets = arig.events("arrival-release-targets")
    assert targets[0]["targets"] == [] and targets[0]["kept"] == ["comfy"]
    kept = [e for e in arig.events("policy-kept") if e.get("reason") == "studio"]
    assert len(kept) == 1 and kept[0]["workload"] == "comfy"
    assert "doesn't cover" in kept[0]["why"] and "Unsloth Studio" in kept[0]["why"]
    assert not arig.arb.events.open_asks()                   # a refusal never becomes a question


def test_ask_me_asks_once_and_unloads_when_the_answer_arrives(arig):
    """The cell says Ask me: one question about this load, nothing released while it is open, and the
    answer is what unloads it. The load itself keeps running meanwhile, exactly as a game's release
    does, and a second poll doesn't ask the owner the same thing a second time."""
    arig.arb.workloads["comfy"].asks_for = ("studio",)
    loaded(arig)
    check(arig.arb, n=3)
    asks = arig.events("ask")
    assert len(asks) == 1, asks
    assert asks[0]["requester"] == "studio" and asks[0]["workload"] == "comfy"
    assert asks[0]["scope"].startswith("ar")                 # this arrival's, not any studio load
    assert arig.comfy.vram == COMFY_MODEL_VRAM               # nothing without the answer
    assert not arig.events("arrival-release")
    assert not arig.events("arrival-release-waiting")        # a question isn't a workload holding on
    assert arig.studio.loaded is True                        # the load carries on meanwhile
    code, view = arig.arb.answer_ask(asks[0]["id"], "release")
    assert code == 200
    check(arig.arb)
    assert arig.comfy.vram == COMFY_CONTEXT_VRAM
    assert len(arig.events("ask")) == 1                      # one question for the whole episode


def test_an_ask_ends_with_the_arrival_it_was_asked_for(arig):
    """The question is about this load sharing the card. Once the conflict is over - here the newcomer
    unloaded - there is nothing left for an answer to free, and an open question would be a countdown
    and a button for a situation that has already gone."""
    arig.arb.workloads["comfy"].asks_for = ("studio",)
    loaded(arig)
    check(arig.arb)
    ask = arig.events("ask")[0]
    assert arig.arb.events.open_asks()
    arig.studio.loaded = False
    check(arig.arb)
    assert arig.events("arrival-end")
    assert not arig.arb.events.open_asks()
    closed = [e for e in arig.events("ask-end") if e["id"] == ask["id"]]
    assert len(closed) == 1 and closed[0]["closed_by"] == "arrival-ended"
    check(arig.arb)
    assert arig.comfy.vram == COMFY_MODEL_VRAM               # never released: nobody said to


def test_two_workloads_already_on_the_card_are_not_an_arrival(arig):
    """The first pass after a start records the card and acts on nothing. Two models already sitting
    there together - `watch` restarted, or a box that came up with both loaded - isn't news, and one
    reading cannot say which of the two arrived: acting on it would unload a model that has been
    resident since before this watcher existed."""
    arig.comfy.vram = COMFY_MODEL_VRAM
    arig.studio.loaded = True
    check(arig.arb, n=3)
    assert not arig.events("arrival")
    assert not arig.events("arrival-release")
    assert arig.comfy.vram == COMFY_MODEL_VRAM
    assert arig.studio.loaded is True


def test_a_workload_with_a_gate_of_its_own_is_never_an_arrival(arig):
    """Strata's hook and ComfyUI's plugin took the GPU lock and made room before they loaded: a
    workload with one of those arriving beside another is a handover in progress, and detecting it
    would race it. Only a `hold` gate - nothing inside it that asks - counts.

    This is the shape of a real handover going the other way: ComfyUI resident, then Strata's model
    loads. Nothing is unloaded for it and nothing is logged as an arrival."""
    arig.comfy.vram = COMFY_MODEL_VRAM
    check(arig.arb)
    arig.strata.loaded = True
    check(arig.arb, n=2)
    assert not arig.events("arrival")
    assert arig.comfy.vram == COMFY_MODEL_VRAM               # no release, no warning
    assert arig.strata.loaded is True


def test_two_watchers_release_once_and_ask_once(arig):
    """`vhotseat watch` and the dashboard's background check both poll, both notice the same load,
    and both act. The episode, its targets and the question come back from the event log, and the
    release re-reads the card inside the GPU lock - so the one that arrives second finds a card that
    is already clear and writes nothing."""
    twin(arig)
    asks_first(arig)
    check(arig.arb)
    check(arig.arb2)                     # both have now seen the card without the newcomer
    arig.comfy.vram = COMFY_MODEL_VRAM
    arig.studio.loaded = True
    check(arig.arb)
    check(arig.arb2)
    assert len(arig.events("arrival")) == 1
    assert len(arig.events("ask")) == 1                     # one question, not one per watcher
    assert len(arig.events("arrival-release-targets")) == 1
    assert not arig.events("arrival-release")               # both are waiting on the one answer
    arig.arb.answer_ask(arig.events("ask")[0]["id"], "release")
    check(arig.arb)
    check(arig.arb2)
    assert arig.comfy.vram == COMFY_CONTEXT_VRAM
    assert len(arig.events("arrival-release")) == 1
    assert arig.comfy.calls.count("POST /free") == 1


def test_a_workload_that_never_gives_its_memory_back_is_asked_three_times(arig, monkeypatch):
    """An unload that answers "unloaded" and keeps holding its VRAM is not a busy moment: it is a
    release that does not work. Going back for it every poll for as long as two models share the card
    spends that time taking the GPU lock for nothing, so the arrival stops asking after three tries
    and the last row says so. The card decides that, not the server's reply."""
    arig.strata.loaded = True
    # The lie: the card keeps showing strata's weights whatever its server says after a successful
    # /unload, and its own `confirm:` is set so the release itself considers itself done - the shape
    # of a node that doesn't hand memory back.
    class Card:
        vram = STRATA_VRAM

    monkeypatch.setattr(arig.gpu, "strata", Card())
    arig.arb.workloads["strata"].spec.setdefault("confirm", {})["vram_at_most"] = STRATA_VRAM
    check(arig.arb)                              # ComfyUI is on its CUDA context only: not a target
    arig.studio.loaded = True
    check(arig.arb, n=4)
    wait = [w for w in arig.events("arrival-release-waiting") if w.get("stuck")]
    assert [w["attempts"] for w in wait] == [1, 2, 3]
    assert all(w["held_mib"] == STRATA_VRAM for w in wait)
    assert "still holds 14000 MiB" in wait[0]["why"]
    assert "3 tries" in wait[-1]["why"]
    assert not arig.events("arrival-release")               # never recorded as released
    check(arig.arb)
    assert len([w for w in arig.events("arrival-release-waiting") if w.get("stuck")]) == 3
    assert arig.strata.calls.count("POST /unload") == 3      # asked three times, then left alone


def test_the_conflict_is_over_when_the_newcomer_unloads(arig):
    """The episode is the conflict, not the workload: once the newcomer's model is gone there is
    nothing left to make room for, so the arrival ends and later passes stop taking the GPU lock and
    reading the card for it."""
    loaded(arig)
    check(arig.arb)
    assert arig.comfy.vram == COMFY_CONTEXT_VRAM
    arig.studio.loaded = False
    check(arig.arb)
    end = arig.events("arrival-end")
    assert len(end) == 1 and "Unsloth Studio unloaded" in end[0]["reason"]
    arig.comfy.vram = COMFY_MODEL_VRAM                      # owner loads a workflow again
    check(arig.arb, n=2)
    assert len(arig.events("arrival")) == 1                 # nothing reopened for it
    assert len(arig.events("arrival-release")) == 1


def test_status_names_the_load_that_asked_for_nothing(arig):
    """An owner reading `vhotseat status` in a terminal gets the same account the log has: which of
    the two on the card asked for nothing, and what is still holding because of its own policy. A
    page that shows it and a `status` that doesn't is two answers, and the terminal one is the one
    used while something is going wrong. It is shown while the arrival lasts - the same window in
    which a claim's `freed for:` line is shown - and not after the conflict has sorted itself out."""
    arig.arb.workloads["comfy"].yields_to = ("strata", "claims", "pressure")
    loaded(arig)
    check(arig.arb)
    out = arig.arb.describe(arig.arb.probe())
    line = next((ln for ln in out.splitlines() if "arrival:" in ln), "")
    assert "studio - kept for its policy: comfy" in line, out
    arig.studio.loaded = False
    check(arig.arb)
    assert not any("arrival:" in ln for ln in arig.arb.describe(arig.arb.probe()).splitlines())


def test_the_page_still_answers_while_an_arrival_is_open(arig):
    """`/api/status` carries an open arrival the way it carries an open claim, and it is something
    JSON can hold: an open claim used to kill the response in json.dumps and leave the page with a
    closed connection, so the same guard applies to these rows. ComfyUI's cell keeps it loaded here,
    which is also the case where the conflict - and the row - lasts."""
    arig.arb.workloads["comfy"].yields_to = ("strata", "claims", "pressure")
    dash = web.Dashboard(arig.arb)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        loaded(arig)
        check(arig.arb)
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/status") as r:
            body = json.loads(r.read())
    finally:
        srv.shutdown()
        srv.server_close()
    assert arig.comfy.vram == COMFY_MODEL_VRAM
    assert body["mode"] == "conflict"
    assert body["arrival_state"]["studio"]["kept"] == ["comfy"]
    assert body["arrival_state"]["studio"]["targets"] == []


def test_an_arrival_is_answered_about_the_card_the_page_is_showing(arig):
    """The dashboard has just read the card for its own status, and that is the card an arrival is an
    arrival about: a second reading a moment later can already disagree - the owner closed what had
    just loaded - and then the watcher would act on a conflict the page isn't showing. So the pass gets
    the reading the page has, and takes no reading of its own.
    """
    arig.comfy.vram = COMFY_MODEL_VRAM
    check(arig.arb)                                   # the pass that records what was on the card
    arig.studio.loaded = True                         # Studio loads, on top of ComfyUI
    gpu = arig.arb.snapshot()
    card = ([n for n, w in arig.arb.workloads.items() if w.active(w.probe(gpu), gpu)], gpu)
    assert "studio" in card[0] and "comfy" in card[0]
    arig.studio.loaded = False                        # and it is gone again before the watcher looks
    web.watch_pass(arig.arb, arig.arb.claim_hits(), card)   # the pass the dashboard would run
    assert len(arig.events("arrival")) == 1
    assert arig.events("arrival")[0]["free_mib"] == gpu["free_mib"]   # that reading, not a new one
    assert arig.comfy.vram == COMFY_CONTEXT_VRAM      # and the room is made for the load it saw
