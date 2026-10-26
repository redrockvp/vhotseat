"""Asking the owner before unloading: the question in the log, the answer that comes back, and
what each path does while it waits.

`asks_for:` is a third answer beside `yields_to:` and the two it already had: give way, keep, or
ask me first. The first two were part A. This is the third: an ask is a row in the event log,
whoever gets there first opens the one question per (workload, reason) pair, and the answer - the
owner's click on the dashboard, their click on a notification's button, or nobody answering in
time - is what the release then acts on.

What the paths cost each other is the part worth pinning down. A load nobody chose (a gate hook, a
guard's request, a proxy's seat) waits for the answer, because nothing is running that an unload
would cut off and the newcomer was going to be allowed or refused either way. A claim and a
pressure episode run meanwhile: the game is not held up by a question, it is simply short of that
much card until an answer arrives. And an answer is consent for the game that was playing when it
was given, never standing permission for the next one to match the same rule.

The notification is the fake in fakes.py, which conftest installs in every arbiter the suite
builds: these tests read the question in the words it was asked in and press its buttons, and no
test can pop a toast on whoever is running the suite. Nothing here writes anyone's real config -
the dashboard's file is always a copy in `tmp_path`.
"""

import re
import threading
import time

import pytest
from fakes import COMFY_MODEL_VRAM, STRATA_VRAM
from test_claim_release import NAME, NOTE, claim_rule, running_render
from test_policy_api import get, post, rig_config_text
from test_pressure_episode import scene

from vhotseat import api, notify, policy, setup, web
from vhotseat.lock import Held


@pytest.fixture
def served(rig, tmp_path):
    """The rig behind a real dashboard, with the config it was built from written out beside it:
    the file the page's two card-touching POSTs answer from, and the one a save rewrites. It is
    always a copy in `tmp_path` - the dashboard writes the owner's config, and a test must not."""
    from http.server import ThreadingHTTPServer
    path = tmp_path / "config.yaml"
    path.write_text(rig_config_text(rig), encoding="utf-8")
    rig.arb.config_path = path
    dash = web.Dashboard(rig.arb)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def ask_first(rig, **per):
    """Who wants asking first before it is unloaded, on the live arbiter: strata=["comfy"] means a
    ComfyUI handover asks Strata first. Same shape as `ask_first` in tests/test_policy.py."""
    for name, spec in per.items():
        rig.arb.workloads[name].asks_for = tuple(spec)


def rows(rig, kind):
    return [e for e in rig.events() if e["event"] == kind]


def fresh_process(rig):
    """A second arbiter over the same log and the same state dir: what a restarted `watch`, or the
    dashboard beside it, or a gate in a command someone just typed, is."""
    return rig.host.install(setup.build(rig.cfg, gpu=rig.gpu))


def wait_for(what, what_says, within=6.0):
    """Wait for a thing to exist and hand it back. An ask a thread is about to open isn't
    answerable until it is in the log, and a test that writes its answer first is answering a
    question nobody asked."""
    deadline = time.monotonic() + within
    while True:
        got = what()
        if got:
            return got
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what_says} never happened within {within} s")
        time.sleep(0.01)


def in_thread(what):
    """Run something that may wait, and hand back (thread, result box). Asking makes the caller
    wait for a person, and the person in these tests is the test."""
    box = {}

    def run():
        try:
            box["out"] = what()
        except BaseException as e:                            # noqa: BLE001 - the test reads it back
            box["error"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, box


def join(th, box, within=6.0):
    th.join(within)
    assert not th.is_alive(), "the caller is still waiting for an answer"
    if "error" in box:
        raise box["error"]
    return box["out"]


def open_ask(rig, workload, requester):
    return lambda: rig.arb.events.open_ask(workload, requester)


# --- the question ---------------------------------------------------------------------


def test_one_question_per_pair_is_open_in_the_log_and_shared_by_every_process(rig):
    """A queue that drains fifty times asks the owner once, and the two processes that can both
    act on this box ask once between them: the open question is the log's, so whoever arrives
    second joins it instead of writing a second one - which matters most when the two answers
    would disagree."""
    rig.strata.loaded = True
    first = rig.arb.ask_now("strata", "comfy", held_mib=STRATA_VRAM, source="guard")
    assert rig.arb.ask_now("strata", "comfy", held_mib=STRATA_VRAM, source="guard")["id"] == first["id"]
    other = fresh_process(rig)
    assert other.events.open_ask("strata", "comfy")["id"] == first["id"]
    assert other.ask_now("strata", "comfy", held_mib=STRATA_VRAM, source="gate")["id"] == first["id"]
    assert len(rows(rig, "ask")) == 1
    # A different reason is a different question: what a game costs and what a handover costs are
    # two decisions, and the owner may well answer them differently.
    game = rig.arb.ask_now("strata", policy.CLAIMS, held_mib=STRATA_VRAM, source="claim",
                           name=NAME, note=NOTE)
    assert game["id"] != first["id"] and len(rows(rig, "ask")) == 2
    # The question is one per (workload, reason), and a second workload asked about is a second
    # question: two toasts, two answers, one for each model on the card.
    rig.comfy.vram = COMFY_MODEL_VRAM
    assert rig.arb.ask_now("comfy", "strata", held_mib=COMFY_MODEL_VRAM, source="gate")["id"]
    assert len(rig.arb.open_asks()) == 3


def test_the_question_says_what_is_holding_and_when_it_was_last_working(rig):
    """The words are the decision: how much card would come free, and whether the workload is in
    the middle of something. A workload nobody recorded working is never shown as idle, because
    "it looks idle" is exactly how an agent gets unloaded between two of its steps."""
    rig.strata.loaded = True
    rig.arb.note_used("strata", time.time() - 40)
    asked = rig.arb.ask_now("strata", "comfy", held_mib=STRATA_VRAM, source="guard")
    text = rig.arb.notify.texts()[0]
    assert asked["last_used"] and asked["timeout_s"] == 120 and asked["fallback"] == "keep"
    assert text["headline"] == "ComfyUI wants the GPU"
    # The stamp, in the tense the decision needs. A row's timestamp only goes to the second, so
    # the figure is that accurate: what is being asserted is that it came from the stamp and is
    # recent, not the millisecond.
    assert re.search(r"Strata is holding 13\.7 GB and used (39|40) s ago", text["detail"])
    assert text["no_answer"] == "No answer in 2 min: Strata stays loaded."
    assert text["buttons"][policy.RELEASE] == "Unload now"
    assert text["buttons"][policy.KEEP] == "Keep Strata"
    # Nothing recorded it working: said as unknown, never as idle.
    rig.comfy.vram = COMFY_MODEL_VRAM
    other = rig.arb.ask_now("comfy", "strata", held_mib=COMFY_MODEL_VRAM, source="gate")
    assert other["last_used"] is None
    assert "ComfyUI is holding 6.8 GB and nothing has recorded it working since this started" \
        in rig.arb.notify.texts()[1]["detail"]


def test_the_watcher_stamps_which_workloads_are_working_and_the_question_uses_it(rig, monkeypatch):
    """The question's "used 40 s ago" comes from the watchers, not from the process asking: on this
    box `watch` may be the one that saw the job and the dashboard the one that asks, so the answer
    goes in the shared state file. An idle workload is not stamped, and neither is one whose poll
    failed - a failed poll is not "idle"."""
    running_render(rig)                       # a ComfyUI job on the fake's executor
    assert rig.arb.workloads["comfy"].busy() and rig.arb.workloads["strata"].busy() is None
    web.note_work(rig.arb)
    stamps = rig.arb.last_used()
    assert set(stamps) == {"comfy"} and rig.arb.used_ago("comfy") < 5
    assert rig.arb.describe(rig.arb.probe()).splitlines()  # a box that has stamped says so
    # A second process's older look at the same job loses to the newer stamp rather than putting
    # it back, and its stamp for a different workload survives the merge.
    other = fresh_process(rig)
    other.note_used("comfy", stamps["comfy"] - 100)
    other.note_used("strata", time.time() - 5)
    assert rig.arb.last_used()["comfy"] == pytest.approx(stamps["comfy"])
    assert set(rig.arb.last_used()) == {"comfy", "strata"}
    # A driver that raises on the question is skipped, and doesn't stop the ones beside it.
    broken = rig.arb.workloads["comfy"]
    monkeypatch.setattr(broken, "busy", lambda: (_ for _ in ()).throw(RuntimeError("port gone")))
    before = rig.arb.last_used()
    web.note_work(rig.arb)
    assert rig.arb.last_used() == before


def test_an_ask_pair_is_only_asked_about_when_the_config_says_ask(rig):
    """`asks_for:` is the third answer, not a wrapper around the other two: a pair it names is
    asked about whatever `yields_to:` says - including an empty `yields_to:`, where asking is the
    only way it would ever come off the card - and a pair it doesn't name is decided on
    `yields_to:` alone."""
    rig.strata.loaded = True
    gpu = rig.arb.snapshot()
    assert rig.arb.ask_for("comfy", gpu) == [] and rig.arb.refuse_for("comfy", gpu) == []
    ask_first(rig, strata=["comfy", "claims"])
    assert rig.arb.ask_for("comfy", gpu) == ["strata"] and rig.arb.refuse_for("comfy", gpu) == []
    # Not named in `asks_for:` is not asked about, and the pair falls back to `yields_to:`: comfy
    # gives way, while the claim - which doesn't - is the question.
    assert rig.arb.ask_for(policy.CLAIMS, gpu) == ["strata"]
    rig.arb.workloads["strata"].yields_to = ()      # refuses nothing outright: asking is the way
    assert rig.arb.ask_for("comfy", gpu) == ["strata"] and rig.arb.refuse_for("comfy", gpu) == []
    rig.arb.workloads["strata"].asks_for = ()       # no question: now the empty list refuses
    assert rig.arb.ask_for("comfy", gpu) == [] and rig.arb.refuse_for("comfy", gpu) == ["strata"]


# --- the answer, and what it is worth ---------------------------------------------------


def test_answering_release_lets_the_waiting_load_through(rig):
    """A load nobody chose is the one that can wait: nothing is running that an unload would cut
    off, and the newcomer was being allowed or refused either way. The answer is the only thing
    that ends the wait, and it comes back through the log rather than down the request."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = wait_for(open_ask(rig, "strata", "comfy"), "the guard's request", 6)
    assert rig.strata.loaded, "the asking unloaded a model before it was answered"
    assert rig.arb.open_asks()[0]["left_s"] > 0
    code, body = rig.arb.answer_ask(asked["id"], policy.RELEASE)
    assert code == 200 and body["ask"]["answer"] == "release" and body["ask"]["by"] == "owner"
    assert body["ask"]["answer_says"] == "Strata unloads"
    assert join(th, box) == 0
    assert not rig.strata.loaded and "POST /unload" in rig.strata.calls
    assert not rows(rig, "policy-refused") and not rig.arb.open_asks()
    assert [(r["answer"], r["by"]) for r in rows(rig, "ask-answer")] == [("release", "owner")]


def test_answering_keep_is_the_same_refusal_in_the_owner_s_own_words(rig):
    """The refusal is the one that was always there; only the sentence about it changes. An owner
    who clicked Keep reads the next refusal as their decision rather than a rule they forgot they
    wrote - and the ask closes, so the next request asks again instead of obeying this answer
    forever."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = wait_for(open_ask(rig, "strata", "comfy"), "the guard's request")
    assert rig.arb.answer_ask(asked["id"], policy.KEEP)[0] == 200
    assert join(th, box) == 1
    assert rig.strata.loaded and "POST /unload" not in rig.strata.calls
    got = rows(rig, "policy-refused")
    assert len(got) == 1 and got[0]["workloads"] == ["strata"]
    assert "its asks_for asks ComfyUI first, and you chose to keep it loaded" in got[0]["why"]
    assert not rig.arb.open_asks()
    # And the next request is a fresh question.
    th2, box2 = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    assert wait_for(open_ask(rig, "strata", "comfy"), "the second request's question")["id"] != asked["id"]
    rig.arb.answer_ask(rig.arb.events.open_ask("strata", "comfy")["id"], policy.RELEASE)
    assert join(th2, box2) == 0 and not rig.strata.loaded


def test_the_first_answer_is_the_answer_and_a_second_is_refused(rig):
    """A notification's button and the dashboard's answer the same row, and whoever gets there
    first wins: the owner clicking Keep a second before a watcher gives up and applies the fallback
    gets their own answer, and the fallback row is then simply ignored. A second answer is refused
    rather than reconciled, because two answers to one question is a bug to report."""
    rig.strata.loaded = True
    asked = rig.arb.ask_now("strata", "comfy", held_mib=STRATA_VRAM, source="guard")
    assert rig.arb.notify.click(asked["id"], policy.KEEP)
    assert rig.arb.answer_ask(asked["id"], policy.RELEASE)[0] == 409
    assert rig.arb.answer_ask(asked["id"], policy.KEEP)[0] == 409
    assert [(r["answer"], r["by"]) for r in rows(rig, "ask-answer")] == [("keep", "owner")]
    # A fallback can't overwrite a human being either: an expired ask that has an answer is left
    # alone, and expiring asks finds nothing to do.
    rig.arb.ask["timeout_s"] = 0
    assert rig.arb.expire_asks() == []
    assert rig.arb.events.ask_by_id(asked["id"])["by"] == "owner"
    assert rig.arb.answer_ask("nosuchask", policy.KEEP)[0] == 404
    assert rig.arb.answer_ask(asked["id"], "maybe")[0] == 400
    assert rig.arb.answer_ask(asked["id"], policy.KEEP, by="a_stranger")[0] == 400


def test_nobody_answering_in_time_is_answered_by_the_fallback(rig):
    """An ask that never closes is a release owed to a workload nobody is going to unload, so the
    wait has a length and the fallback has a name - and the row says it wasn't the owner who
    answered, which is the one unload in the log nobody agreed to."""
    ask_first(rig, strata=["comfy"])
    rig.arb.ask["timeout_s"] = 0.05
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy", "--request") == 1
    assert rig.strata.loaded and "POST /unload" not in rig.strata.calls
    assert [(r["answer"], r["by"]) for r in rows(rig, "ask-answer")] == [("keep", "timeout")]
    assert "nobody answered in time, so its ask: fallback (keep) decided" \
        in rows(rig, "policy-refused")[0]["why"]
    assert not rig.arb.open_asks()


def test_a_fallback_of_release_gives_the_card_away_when_nobody_answers(rig):
    """`fallback: release` is the other half of the same decision: the owner who would rather
    their queue never sat in front of a game says so once in the config instead of once per
    question. It is still recorded as an answer nobody gave, so the log never reads as though a
    person chose it."""
    ask_first(rig, strata=["comfy"])
    rig.arb.ask.update({"timeout_s": 0.05, "fallback": policy.RELEASE})
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy", "--request") == 0
    assert not rig.strata.loaded and "POST /unload" in rig.strata.calls
    assert [(r["answer"], r["by"]) for r in rows(rig, "ask-answer")] == [("release", "timeout")]
    assert not rows(rig, "policy-refused")


def test_a_question_keeps_the_wait_it_was_asked_with(rig):
    """The timeout and the fallback are copied into the row when it opens, so an owner who saves a
    different pair of numbers while a question is standing doesn't move the goalposts under the one
    waiting - and what fires on a timeout is what they were told when they were asked."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = wait_for(open_ask(rig, "strata", "comfy"), "the guard's request")
    assert (asked["timeout_s"], asked["fallback"]) == (120, "keep")
    rig.arb.ask.update({"timeout_s": 0.05, "fallback": policy.RELEASE})
    assert rig.arb.events.ask_by_id(asked["id"])["fallback"] == "keep"
    assert rig.arb.answer_ask(asked["id"], policy.RELEASE)[0] == 200
    assert join(th, box) == 0 and not rig.strata.loaded


def test_answering_takes_no_gpu_lock_so_a_question_survives_a_handover(rig):
    """The answer is written while a handover holds the card: an answer that had to wait for the
    GPU lock could not be written during the very release it is deciding, which is when a person is
    most likely to be answering it. The release is owed by whoever asked, so the answer is only a
    row, and taking the lock to write it would make the asking unanswerable."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = wait_for(open_ask(rig, "strata", "comfy"), "the guard's request")
    # Ours, not the ask's: the row is in the log a moment before the opener gives the lock back.
    def ours():
        got = rig.arb.lock.try_take("switch comfy")
        return got if isinstance(got, Held) else None

    held = wait_for(ours, "this test taking the GPU lock")
    assert rig.arb.answer_ask(asked["id"], policy.RELEASE)[0] == 200
    held.__exit__(None, None, None)
    assert join(th, box) == 0 and not rig.strata.loaded


def test_no_notifications_still_asks_where_a_person_can_see_it(rig):
    """A `watch` at logon over a disconnected RDP session, a Linux box, a machine without the toast
    library: the asking is the log and the dashboard, and a notification is only a way in. Losing
    one changes what is shown, never what is asked - and the status says so, because waiting for a
    popup that was never arriving is its own kind of hang."""
    ask_first(rig, strata=["comfy"])
    rig.arb.notify = notify.NullNotifier("no desktop session for this process")
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = wait_for(open_ask(rig, "strata", "comfy"), "the guard's request")
    assert asked["workload"] == "strata"
    s = rig.arb.probe()
    assert s["notifications"] == {"available": False, "how": "none",
                                  "why": "no desktop session for this process"}
    assert [a["id"] for a in s["asks"]] == [asked["id"]]
    assert "no notification from here" in rig.arb.describe(s)
    rig.arb.answer_ask(asked["id"], policy.RELEASE)
    assert join(th, box) == 0 and not rig.strata.loaded


# --- the paths that wait -----------------------------------------------------------------


def test_a_gate_hook_waits_for_the_answer_instead_of_refusing_the_agent(rig):
    """The load is inside the hook, so the waiting has to be too: the point of an Ask me at a gate
    is that the owner gets asked rather than the agent getting a refusal. The release the answer
    agrees to then happens under the lock, in the same hook, as it always did."""
    ask_first(rig, comfy=["strata"])
    rig.comfy.vram = COMFY_MODEL_VRAM
    th, box = in_thread(lambda: rig.arb.gate("strata"))
    asked = wait_for(open_ask(rig, "comfy", "strata"), "the gate's question")
    assert rig.comfy.vram == COMFY_MODEL_VRAM, "the gate unloaded the holder before asking"
    assert rig.arb.answer_ask(asked["id"], policy.RELEASE)[0] == 200
    assert join(th, box) is True
    assert rig.comfy.vram == 400 and not rows(rig, "policy-refused")


def test_a_gate_that_runs_out_of_its_own_time_leaves_the_question_open(rig):
    """The hook's timeout is the gate's, not the ask's: running out of it stops this waiter and
    refuses this load, but the question stays open - timed from when it was asked - for the next
    request and for whoever is at the dashboard. A hook that closed the ask on its own deadline
    would be answering the owner's question for them."""
    ask_first(rig, comfy=["strata"])
    rig.comfy.vram = COMFY_MODEL_VRAM
    rig.arb.workloads["strata"].gate = {"timeout": 0.02}
    assert rig.arb.gate("strata") is False
    asked = rig.arb.events.open_ask("comfy", "strata")
    assert asked and not asked.get("answer") and not rows(rig, "ask-answer")
    assert "its asks_for asks Strata first, and the answer hasn't come yet" \
        in rows(rig, "policy-refused")[0]["why"]
    # The next request joins that question rather than opening a second one, and the answer that
    # eventually comes is what decides it.
    rig.arb.workloads["strata"].gate = {"timeout": 60}
    th, box = in_thread(lambda: rig.arb.gate("strata"))
    assert wait_for(open_ask(rig, "comfy", "strata"), "the second request")
    assert len(rows(rig, "ask")) == 1
    rig.arb.answer_ask(asked["id"], policy.RELEASE)
    assert join(th, box) is True and rig.comfy.vram == 400


def test_a_proxy_waits_and_a_second_request_joins_the_question(rig):
    """The library's Seat is what a proxy in front of a server with no load hook runs: it waits on
    the same log, needs no channel back to anything, and stamps the workload as used because on
    that box nothing else knows a request arrived at all. And the queue behind it asks once: fifty
    requests waiting for one card are one question to the owner."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    seat = api.Seat(rig.arb)
    th, box = in_thread(lambda: seat.acquire("comfy"))
    asked = wait_for(open_ask(rig, "strata", "comfy"), "the proxy's question")
    assert rig.arb.last_used().get("comfy"), "the proxy didn't note its own request arriving"
    th2, box2 = in_thread(lambda: rig.arb.ask_before_request("comfy", source="gate"))
    assert wait_for(lambda: th.is_alive() and th2.is_alive() and len(rows(rig, "ask")) == 1
                    and "joined",
                    "the second request joining the one question")
    assert len(rows(rig, "ask")) == 1
    rig.arb.answer_ask(asked["id"], policy.RELEASE)
    assert join(th, box) == []
    consent, blockers, why = join(th2, box2)
    assert consent == {"strata"} and blockers == [] and why is None
    assert len(rows(rig, "ask")) == 1 and not rig.strata.loaded


# --- the paths that never wait -------------------------------------------------------------


def test_a_game_runs_while_the_answer_is_outstanding(rig, monkeypatch):
    """A claim is never refused and never held up: the game starts with whatever is left, the
    question goes out with the numbers in it, and the release the owner agrees to is owed to that
    game - so it happens on the next look, through the same release path as any other target."""
    claim_rule(rig, monkeypatch)
    ask_first(rig, strata=["claims"])
    rig.strata.loaded = True
    rig.comfy.vram = COMFY_MODEL_VRAM
    web.watch(rig.arb, polls=1)
    assert rig.comfy.vram == 400 and rig.strata.loaded, "the ask unloaded a model before it was asked"
    asked = rig.arb.events.open_ask("strata", policy.CLAIMS)
    assert asked["name"] == NAME and asked["note"] == NOTE
    assert [r["workloads"] for r in rows(rig, "claim-release")] == [["comfy"]]
    assert rig.arb.claim_release({"name": NAME, "note": NOTE}, only=["strata"])["released"] == []
    assert rig.arb.answer_ask(asked["id"], policy.RELEASE)[0] == 200
    web.watch(rig.arb, polls=1)
    assert not rig.strata.loaded
    assert [w["workload"] for w in rows(rig, "ask-answer")] == ["strata"]
    assert "Strata" not in (rows(rig, "claim-release-targets")[0]["kept"] or [])


def test_an_answer_is_consent_for_the_game_that_was_playing(rig, monkeypatch):
    """"Unload Strata" is an answer about this game. A later game that matches the same rule gets
    its own question: an answer left in the log must never read as standing permission, which is
    why an ask is scoped to the claim it belongs to and not to the pair."""
    procs = claim_rule(rig, monkeypatch)
    ask_first(rig, strata=["claims"])
    rig.strata.loaded = True
    web.watch(rig.arb, polls=1)
    asked = rig.arb.events.open_ask("strata", policy.CLAIMS)
    rig.arb.answer_ask(asked["id"], policy.RELEASE)
    web.watch(rig.arb, polls=1)
    assert not rig.strata.loaded

    rig.strata.loaded = True
    rig.comfy.vram = COMFY_MODEL_VRAM
    del procs[4242]                                   # the game closes...
    web.watch(rig.arb, polls=1)
    procs[9999] = {"pid": 9999, "ppid": 4, "name": NAME, "exe": r"D:\Libraries\elden\EldenRing.exe"}
    web.watch(rig.arb, polls=1)                       # ...and another one starts, a second later
    again = rig.arb.events.open_ask("strata", policy.CLAIMS)
    assert again["id"] != asked["id"], "the new game inherited the old game's answer"
    assert again["pid"] == 9999 and rig.strata.loaded


def test_a_keep_for_a_claim_is_said_once_however_often_the_watcher_polls(rig, monkeypatch):
    """A workload the owner has answered "keep" stays holding memory the game may be short of,
    which is worth one row and one banner. It is not worth one per poll: an open claim is re-read
    every couple of seconds, and a warning that repeats that fast is a warning nobody reads."""
    claim_rule(rig, monkeypatch)
    ask_first(rig, strata=["claims"])
    rig.strata.loaded = True
    web.watch(rig.arb, polls=1)
    rig.arb.answer_ask(rig.arb.events.open_ask("strata", policy.CLAIMS)["id"], policy.KEEP)
    for _ in range(3):
        web.watch(rig.arb, polls=1)
    kept = rows(rig, "policy-kept")
    assert [k["workload"] for k in kept] == ["strata"]
    assert kept[0]["reason"] == policy.CLAIMS and "you chose to keep it loaded" in kept[0]["why"]


def test_no_answer_for_a_claim_closes_on_the_fallback_not_forever(rig, monkeypatch):
    """A game outlives the ask's timeout: the watcher's next pass applies the fallback rather than
    leaving the question open and the game short indefinitely. `keep` is the safe answer, and it
    is recorded as one nobody gave."""
    claim_rule(rig, monkeypatch)
    ask_first(rig, strata=["claims"])
    rig.arb.ask["timeout_s"] = 0.05
    rig.strata.loaded = True
    web.watch(rig.arb, polls=1)
    assert rig.arb.events.open_ask("strata", policy.CLAIMS)
    web.watch(rig.arb, polls=1)
    assert [(r["answer"], r["by"]) for r in rows(rig, "ask-answer")] == [("keep", "timeout")]
    assert rig.strata.loaded and [k["workload"] for k in rows(rig, "policy-kept")] == ["strata"]


def test_an_episode_asks_before_it_takes_what_the_owner_wants_asked(rig, monkeypatch):
    """The same shape as a claim, for the app that grew: the episode is owed the release, the
    question goes out on the first pass, and the answer - or the fallback, on a later pass - is
    what closes it. The eviction set is not quietly reduced to whatever doesn't want asking."""
    ask_first(rig, strata=["pressure"])
    scene(rig, monkeypatch)
    assert [n for n, _ in rig.arb.pressure_candidates()] == ["strata"]
    assert rig.arb.pressure_start()["declined"] is None
    asked = rig.arb.events.open_ask("strata", policy.PRESSURE)
    assert asked["name"], "the ask didn't say which app took the card"
    assert rig.strata.loaded and not rows(rig, "pressure-release")
    rig.arb.answer_ask(asked["id"], policy.RELEASE)
    out = rig.arb.pressure_release(rig.arb.events.episode_open())
    assert [r["workload"] for r in out["released"]] == ["strata"] and not rig.strata.loaded


# --- what the owner reads ---------------------------------------------------------------


def test_status_names_the_open_question_and_why_points_at_it(rig, capsys):
    """`status` has to say which workloads are being asked about and which were refused outright -
    the difference between waiting for a person and having decided already - and `why` has to say
    so beside the one the owner can still change their mind about, with the id to answer it by."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    asked = rig.arb.ask_now("strata", "comfy", held_mib=STRATA_VRAM, source="guard")
    assert rig.cli("status") == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("asks:"))  # noqa: E741
    assert "ComfyUI wants the GPU - Strata: answer within " in line and "2 min" in line
    assert "No answer in 2 min: Strata stays loaded." in line
    assert rig.cli("why", "comfy") == 0
    out = capsys.readouterr().out
    assert f"asked already (ask {asked['id']}): ComfyUI is waiting for the answer" in out
    s = rig.arb.probe()
    assert [a["id"] for a in s["asks"]] == [asked["id"]]
    assert s["asks"][0]["requester_label"] == "ComfyUI" and s["asks"][0]["left_s"] > 0
    assert s["asks"][0]["text"]["detail"] == rig.arb.notify.texts()[0]["detail"]
    rig.arb.answer_ask(asked["id"], policy.KEEP)
    assert rig.arb.probe()["asks"] == []
    assert rig.cli("why", "comfy") == 0
    out = capsys.readouterr().out
    # The question is closed and its answer belongs to the request that was waiting, not to this
    # reading of the card: `why` states the rule, which is still "this pair is unloaded only if you
    # say so", and the answer the owner gave is in the log beside the refusal it produced.
    assert "its asks_for asks it first" in out and "asked already" not in out
    assert [(r["answer"], r["by"]) for r in rows(rig, "ask-answer")] == [("keep", "owner")]


def test_the_dashboard_shows_the_question_and_its_button_answers_it(served, rig):
    """The page is the other way in: `/api/status` carries the open question with its words and its
    countdown, and the button posts to the ask's own id - so the numbers an owner answered are the
    numbers they were shown, and an answer can't be applied to a question they never saw."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = wait_for(open_ask(rig, "strata", "comfy"), "the guard's request")
    code, s, _ = get(served + "/api/status")
    assert code == 200
    view = s["asks"][0]
    assert view["id"] == asked["id"] and view["left_s"] > 0
    assert [a["answer"] for a in view["text"]["answers"]] == ["release", "keep"]
    assert view["text"]["buttons"] == rig.arb.notify.texts()[0]["buttons"]
    assert view["label"] == "Strata" and s["notifications"]["available"] is True
    assert post(f"{served}/api/asks/{asked['id']}", {"answer": "release"})[0] == 200
    assert join(th, box) == 0 and not rig.strata.loaded
    assert post(f"{served}/api/asks/{asked['id']}", {"answer": "keep"})[0] == 409
    assert post(f"{served}/api/asks/nosuchask", {"answer": "keep"})[0] == 404
    assert post(f"{served}/api/asks/{asked['id']}", {"answer": "sidestep"})[0] == 400
    assert post(f"{served}/api/asks/{asked['id']}", {"answer": "keep"}, edit=None)[0] == 403


@pytest.mark.parametrize("bad", [{"answer": "maybe"}, {}, {"answer": 1}, "not an object", []])
def test_an_answer_the_page_didnt_send_is_refused_before_it_touches_anything(served, rig, bad):
    """A body that isn't an answer is a 400 that says what was expected: an ask that guessed at a
    malformed answer would unload a model on a typo."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    asked = rig.arb.ask_now("strata", "comfy", held_mib=STRATA_VRAM, source="guard")
    code, out = post(f"{served}/api/asks/{asked['id']}", bad)
    assert code == 400 and out["error"]
    assert rig.arb.events.open_ask("strata", "comfy") and rig.strata.loaded


def test_making_room_now_overrides_the_question_and_writes_no_rule(served, rig):
    """The warning's "make room now" is a person deciding this once: it goes through as an order,
    names the policy it overrode, and asks nothing - and it writes nothing to the config, because
    wanting room today and wanting the rule changed are different answers."""
    grid = get(served + "/api/policy")[1]
    matrix = {row: {col: cell["choice"] for col, cell in cols.items() if col != row}
              for row, cols in grid["matrix"].items()}
    matrix["strata"]["comfy"] = "ask"
    assert post(served + "/api/policy", {"version": grid["version"],
                                         "priorities": [w["workload"] for w in grid["order"]],
                                         "matrix": matrix})[0] == 200
    assert rig.arb.workloads["strata"].asks_for == ("comfy",)
    rig.strata.loaded = True
    asked_before = len(rows(rig, "ask"))
    version_before = get(served + "/api/policy")[1]["version"]
    code, out = post(served + "/api/make-room", {"for": "comfy"})
    assert code == 200 and out["ok"], out
    assert not rig.strata.loaded and "POST /unload" in rig.strata.calls
    assert len(rows(rig, "ask")) == asked_before and not rig.arb.open_asks()
    assert [r["requested"] for r in rows(rig, "policy-override")] == ["comfy"]
    after = get(served + "/api/policy")[1]
    assert after["version"] == version_before
    assert after["matrix"]["strata"]["comfy"]["choice"] == "ask"
    assert post(served + "/api/make-room", {"for": "nosuchworkload"})[0] == 400
    assert post(served + "/api/make-room", {"for": "comfy"}, edit=None)[0] == 403


def test_a_saved_policy_starts_the_asking_without_a_restart(served, rig):
    """The owner sets an Ask me on the dashboard, and the watcher - a process that has been running
    all day - is the one that has to obey it: the save, the reload and the first question are one
    story, and a restart would leave the editor saying one thing and the card doing another."""
    grid = get(served + "/api/policy")[1]
    matrix = {row: {col: cell["choice"] for col, cell in cols.items() if col != row}
              for row, cols in grid["matrix"].items()}
    matrix["strata"]["comfy"] = "ask"
    code, saved = post(f"{served}/api/policy", {"version": grid["version"],
                                                "priorities": [w["workload"] for w in grid["order"]],
                                                "matrix": matrix})
    assert code == 200, saved
    assert rig.arb.workloads["strata"].asks_for == ("comfy",)
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = wait_for(open_ask(rig, "strata", "comfy"), "the question the saved policy owes")
    assert asked["source"] == "guard"
    rig.arb.answer_ask(asked["id"], policy.RELEASE)
    assert join(th, box) == 0 and not rig.strata.loaded
