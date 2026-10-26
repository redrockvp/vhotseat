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

import contextlib
import re
import threading
import time

import pytest
from fakes import COMFY_CONTEXT_VRAM, COMFY_MODEL_VRAM, STRATA_VRAM
from test_claim_release import NAME, NOTE, claim_rule, running_render
from test_policy_api import get, post, rig_config_text
from test_pressure_episode import samples, scene

from vhotseat import api, arbiter, claims, events, host, lock, notify, policy, setup, web
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


@pytest.fixture
def waiters(monkeypatch):
    """The ask ids each request has actually sat down to wait on.

    A row in the log is not yet a request waiting on it: between writing the question and starting
    to poll for its answer, the request isn't waiting for anything, and an answer written in that
    gap closes the question the request then finds already answered - so it asks a fresh one, and
    the test is measuring a second question rather than the one it means to measure. `await_asks`
    notes the ids here as it begins to poll; `awaiting` waits for that."""
    seen: list[str] = []
    real = arbiter.Arbiter.await_asks

    def noting(self, asks, own_deadline=None):
        seen.extend(a["id"] for a in asks)
        return real(self, asks, own_deadline=own_deadline)

    monkeypatch.setattr(arbiter.Arbiter, "await_asks", noting)
    return seen


def awaiting(rig, waiters, workload, requester, within=6.0):
    """The pair's open ask, once the request that asked it is waiting for its answer."""
    asked = wait_for(open_ask(rig, workload, requester),
                     f"the question about {workload} for {requester}", within)
    wait_for(lambda: asked["id"] in waiters, "the request waiting on that question", within)
    return asked


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


def test_answering_release_lets_the_waiting_load_through(rig, waiters):
    """A load nobody chose is the one that can wait: nothing is running that an unload would cut
    off, and the newcomer was being allowed or refused either way. The answer is the only thing
    that ends the wait, and it comes back through the log rather than down the request."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = awaiting(rig, waiters, "strata", "comfy")
    assert rig.strata.loaded, "the asking unloaded a model before it was answered"
    assert rig.arb.open_asks()[0]["left_s"] > 0
    code, body = rig.arb.answer_ask(asked["id"], policy.RELEASE)
    assert code == 200 and body["ask"]["answer"] == "release" and body["ask"]["by"] == "owner"
    assert body["ask"]["answer_says"] == "Strata unloads"
    assert join(th, box) == 0
    assert not rig.strata.loaded and "POST /unload" in rig.strata.calls
    assert not rows(rig, "policy-refused") and not rig.arb.open_asks()
    assert [(r["answer"], r["by"]) for r in rows(rig, "ask-answer")] == [("release", "owner")]


def test_answering_keep_is_the_same_refusal_in_the_owner_s_own_words(rig, waiters):
    """The refusal is the one that was always there; only the sentence about it changes. An owner
    who clicked Keep reads the next refusal as their decision rather than a rule they forgot they
    wrote - and the ask closes, so the next request asks again instead of obeying this answer
    forever."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = awaiting(rig, waiters, "strata", "comfy")
    assert rig.arb.answer_ask(asked["id"], policy.KEEP)[0] == 200
    assert join(th, box) == 1
    assert rig.strata.loaded and "POST /unload" not in rig.strata.calls
    got = rows(rig, "policy-refused")
    assert len(got) == 1 and got[0]["workloads"] == ["strata"]
    assert "its asks_for asks ComfyUI first, and you chose to keep it loaded" in got[0]["why"]
    assert not rig.arb.open_asks()
    # And the next request is a fresh question.
    th2, box2 = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    second = awaiting(rig, waiters, "strata", "comfy")
    assert second["id"] != asked["id"]          # the answer closed that one; this is a new question
    rig.arb.answer_ask(second["id"], policy.RELEASE)
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


def slow_log_reads(monkeypatch):
    """A log read that takes the time a real one takes on a busy disk, which is the moment two
    answerers overlap: both read the ask as open, and only one of them should get to say so."""
    real = events.EventLog._read

    def slow(self):
        lines = real(self)
        time.sleep(0.05)
        return lines

    monkeypatch.setattr(events.EventLog, "_read", slow)


def test_keep_and_release_written_at_the_same_time_leave_one_answer(rig, monkeypatch):
    """The owner clicking Keep and a watcher giving up and applying a fallback Release are separate
    processes on this box, and they can be in flight in the same moment. One answer is written, and
    the caller whose answer didn't get written is told so.

    Two `ask-answer` rows for one ask is not untidiness: whichever path acts next acts on the row its
    own reading happened to see first, so the workload can be unloaded on a fallback after the owner
    said keep, or stay loaded on a keep after the fallback said release - and the log, read back
    afterwards, can be made to say either. The first answer has to win in the file, not in a
    reading of it."""
    rig.strata.loaded = True
    asked = rig.arb.ask_now("strata", "comfy", held_mib=STRATA_VRAM, source="guard")
    slow_log_reads(monkeypatch)
    keep, box_keep = in_thread(lambda: rig.arb.answer_ask(asked["id"], policy.KEEP))
    release, box_release = in_thread(lambda: rig.arb.answer_ask(asked["id"], policy.RELEASE))
    got = [join(keep, box_keep), join(release, box_release)]
    answers = rows(rig, "ask-answer")
    assert len(answers) == 1, f"both answers were written: {[a['answer'] for a in answers]}"
    assert sorted(code for code, _ in got) == [200, 409], f"both callers were told they won: {got}"
    winner = next(body["ask"] for code, body in got if code == 200)
    assert winner["answer"] == answers[0]["answer"] == rig.arb.events.ask_by_id(asked["id"])["answer"]
    loser = next(body for code, body in got if code == 409)
    assert f"was answered {answers[0]['answer']}" in loser["error"]


def test_an_answer_that_couldnt_be_written_is_not_an_answer(rig, monkeypatch):
    """The log can be busy with a writer that has wedged. An answer that didn't get written is then
    refused out loud rather than accepted: a button that disappears while its row is still unsent
    leaves the owner believing the GPU is coming down, and the question they can still answer on the
    dashboard is the one whose answer was never recorded."""
    rig.strata.loaded = True
    asked = rig.arb.ask_now("strata", "comfy", held_mib=STRATA_VRAM, source="guard")

    @contextlib.contextmanager
    def wedged(*args, **kwargs):
        raise lock.Locked("events.jsonl: something else is still writing the log - nothing written")
        yield                                                # unreachable: the turn isn't granted

    monkeypatch.setattr(events.lock, "write_turn", wedged)
    code, body = rig.arb.answer_ask(asked["id"], policy.KEEP)
    assert code == 503 and "still open" in body["error"]
    assert rows(rig, "ask-answer") == []
    assert [a["id"] for a in rig.arb.open_asks()] == [asked["id"]]
    monkeypatch.undo()
    assert rig.arb.answer_ask(asked["id"], policy.KEEP)[0] == 200


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


def test_a_question_keeps_the_wait_it_was_asked_with(rig, waiters):
    """The timeout and the fallback are copied into the row when it opens, so an owner who saves a
    different pair of numbers while a question is standing doesn't move the goalposts under the one
    waiting - and what fires on a timeout is what they were told when they were asked."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = awaiting(rig, waiters, "strata", "comfy")
    assert (asked["timeout_s"], asked["fallback"]) == (120, "keep")
    rig.arb.ask.update({"timeout_s": 0.05, "fallback": policy.RELEASE})
    assert rig.arb.events.ask_by_id(asked["id"])["fallback"] == "keep"
    assert rig.arb.answer_ask(asked["id"], policy.RELEASE)[0] == 200
    assert join(th, box) == 0 and not rig.strata.loaded


def test_answering_takes_no_gpu_lock_so_a_question_survives_a_handover(rig, waiters):
    """The answer is written while a handover holds the card: an answer that had to wait for the
    GPU lock could not be written during the very release it is deciding, which is when a person is
    most likely to be answering it. The release is owed by whoever asked, so the answer is only a
    row, and taking the lock to write it would make the asking unanswerable."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = awaiting(rig, waiters, "strata", "comfy")
    # Ours, not the ask's: the row is in the log a moment before the opener gives the lock back.
    def ours():
        got = rig.arb.lock.try_take("switch comfy")
        return got if isinstance(got, Held) else None

    held = wait_for(ours, "this test taking the GPU lock")
    assert rig.arb.answer_ask(asked["id"], policy.RELEASE)[0] == 200
    held.__exit__(None, None, None)
    assert join(th, box) == 0 and not rig.strata.loaded


def test_no_notifications_still_asks_where_a_person_can_see_it(rig, waiters):
    """A `watch` at logon over a disconnected RDP session, a Linux box, a machine without the toast
    library: the asking is the log and the dashboard, and a notification is only a way in. Losing
    one changes what is shown, never what is asked - and the status says so, because waiting for a
    popup that was never arriving is its own kind of hang."""
    ask_first(rig, strata=["comfy"])
    rig.arb.notify = notify.NullNotifier("no desktop session for this process")
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = awaiting(rig, waiters, "strata", "comfy")
    assert asked["workload"] == "strata"
    s = rig.arb.probe()
    assert s["notifications"] == {"available": False, "how": "none",
                                  "why": "no desktop session for this process"}
    assert [a["id"] for a in s["asks"]] == [asked["id"]]
    assert "no notification from here" in rig.arb.describe(s)
    rig.arb.answer_ask(asked["id"], policy.RELEASE)
    assert join(th, box) == 0 and not rig.strata.loaded


# --- the paths that wait -----------------------------------------------------------------


class FakeMonotonic:
    """A clock the test turns by hand: one `monotonic()` for the arbiter to measure a hook against,
    advanced by the test rather than by waiting. Only `monotonic` is faked - the wall clock the rows
    are stamped with stays real, so a question that is still open and a hook that has run out of time
    are two separate things, which is exactly the pair the hook has to get right."""

    def __init__(self, real):
        self.real, self.t = real, real.monotonic()

    def monotonic(self):
        return self.t

    def time(self):
        return self.real.time()

    def advance(self, seconds):
        self.t += seconds


def test_the_hook_and_the_wait_share_one_deadline(rig, monkeypatch, waiters):
    """`gate.timeout` is the whole hook's budget: the waiting for an answer, and the handover the
    answer authorises, both fit inside it. Measured twice - once for the asking, once started again
    when the answer arrives - the handover is handed a budget the host stopped paying for, and its
    restart guard ("don't start a 20 s restart the gate can't finish") is told a whole timeout is
    left when the waiting spent it. Same rig, same answer: the restart happens when time is left and
    is refused when the waiting used it up.

    The suite runs waits at a tenth (`util.SCALE`), so a 60 s hook budget is 6 of the units this
    clock is turned in, and the restart guard wants 2 of them."""
    clock = FakeMonotonic(time)
    monkeypatch.setattr(arbiter, "time", clock)
    ask_first(rig, comfy=["strata"])
    rig.arb.workloads["strata"].gate = {"timeout": 60}

    def handover(spent):
        """One gate hook: the question asked, `spent` of the hook's budget gone, the owner's answer,
        and what the hook then does."""
        rig.comfy.vram = COMFY_MODEL_VRAM
        rig.host.mem["comfyui"] = {"ram_mib": 6000, "swap_mib": 1000, "file_mib": 0}
        rig.host.available_mib = 42000                      # ComfyUI's process is what's short
        rig.host.on_restart["comfyui"] = lambda: (rig.comfy.restarted(),
                                                  setattr(rig.host, "available_mib", 49000))
        th, box = in_thread(lambda: rig.arb.gate("strata"))
        asked = awaiting(rig, waiters, "comfy", "strata")
        clock.advance(spent)             # the owner takes this long to answer
        assert rig.arb.answer_ask(asked["id"], policy.RELEASE)[0] == 200
        return join(th, box), asked

    ok, _ = handover(spent=0)
    assert ok is True and rig.host.restarts == ["comfyui"], "an answer with time left didn't restart"

    rig.host.restarts = []
    ok, asked = handover(spent=5)        # 5 of the hook's 6: 1 left, and the guard wants 2
    assert ok is False, "the hook started a restart it has no time to finish"
    assert rig.host.restarts == [], "it restarted ComfyUI into a hook the host is about to kill"
    assert "no time left to restart anything" in rows(rig, "strata-load")[-1]["reason"]
    assert rig.arb.events.ask_by_id(asked["id"])["answer"] == policy.RELEASE


def test_a_question_for_a_game_that_has_gone_is_closed_not_carried(rig, monkeypatch):
    """A question is asked about a game process, and it stops meaning anything when that process
    does. Game A asks, the owner says nothing, and A exits mid-countdown: the card used to go on
    showing A's countdown - a button whose consent no release would ever act on - and worse, the
    next game B that matches the same rule was joined to that stale question rather than given its
    own. So the owner clicked "Unload now" on a card that answered 200 and did nothing, because the
    consent it recorded belonged to a process that no longer existed, and only on the next poll did
    B's real question appear. Now the claim ending retires its open asks with a closing row that
    says why, and the new game is asked its own question, with its own notification.

    Retiring is not an answer: the row says the question closed because the thing it was about
    stopped existing, so a log read later cannot mistake it for a `keep` nobody gave, and a question
    that has gone is answered exactly like any other closed one - refused."""
    procs = claim_rule(rig, monkeypatch)
    ask_first(rig, strata=["claims"])
    rig.strata.loaded = True
    web.watch(rig.arb, polls=1)
    asked = rig.arb.events.open_ask("strata", policy.CLAIMS)
    assert asked["pid"] == 4242 and len(rig.arb.notify.asks) == 1

    del procs[4242]                                   # the game closes, unanswered
    web.watch(rig.arb, polls=1)
    assert not rig.arb.events.open_ask("strata", policy.CLAIMS), \
        "the card still asks the owner to decide for a game that is gone"
    ended = rows(rig, "ask-end")
    assert [(r["id"], r["closed_by"]) for r in ended] == [(asked["id"], "claim-ended")]
    assert "exited" in ended[0]["reason"] and ended[0]["workload"] == "strata"
    assert not rows(rig, "ask-answer"), "a question that was never answered recorded an answer"
    assert rig.strata.loaded, "retiring a question unloaded something on its own"
    assert rig.arb.answer_ask(asked["id"], policy.RELEASE)[0] == 409, \
        "a retired question can still be answered"

    procs[9999] = {"pid": 9999, "ppid": 4, "name": NAME, "exe": r"D:\Libraries\elden\EldenRing.exe"}
    web.watch(rig.arb, polls=1)                       # a new game, inside the old timeout
    again = rig.arb.events.open_ask("strata", policy.CLAIMS)
    assert again and again["id"] != asked["id"], "the new game inherited the old game's question"
    assert again["pid"] == 9999
    assert [a["id"] for a in rig.arb.notify.asks] == [asked["id"], again["id"]], \
        "the new game's question was never put in front of the owner"
    assert rig.arb.answer_ask(again["id"], policy.RELEASE)[0] == 200
    web.watch(rig.arb, polls=1)
    assert not rig.strata.loaded and "POST /unload" in rig.strata.calls, \
        "the answer to the new game's own question did not free the card for it"


def claim_two_games(rig, monkeypatch):
    """Two games claiming the card at once - two rules, two processes, both owed the same
    workload - in the fake process table. Returns the table: take one game out of it and that
    claim ends while the other carries on. One question per (workload, reason) cannot serve this:
    the two games want different answers, and one of them would spend the whole game with a
    question that was really asked about the other."""
    arb = rig.arb
    monkeypatch.setattr(arb, "claim_rules", claims.rules(
        [{"name": NAME, "note": NOTE, "on_start": "release"},
         {"name": "Hades.exe", "note": "Hades", "on_start": "release"}], builtin=False))
    monkeypatch.setattr(arb, "claim_ignore", claims.ignores())
    procs = {
        4242: {"pid": 4242, "ppid": 4, "name": NAME, "exe": r"D:\Libraries\elden\EldenRing.exe"},
        9999: {"pid": 9999, "ppid": 7, "name": "Hades.exe", "exe": r"D:\Libraries\hades\Hades.exe"},
    }
    monkeypatch.setattr(host, "processes", lambda: procs)
    return procs


def test_two_games_asking_about_one_workload_each_get_their_own_question(rig, monkeypatch):
    """A question belongs to the thing that wants the memory freed, and that is part of what it is,
    not something checked afterwards: a claim's questions belong to the game process that took the
    card, an episode's to that episode, and a load nobody chose has no scope beyond the requester it
    is named for.

    Two games on one card wanting the same workload unloaded are two things wanting an answer. One
    question between them means one of them runs the whole game with a countdown that was asked
    about the other, an answer that releases for the wrong claim, and - worse, because it looks like
    it worked - an Unload now that answers 200 and does nothing. So both are asked, each with its
    own card and its own toast, and when one game exits it takes only its own question with it."""
    procs = claim_two_games(rig, monkeypatch)
    ask_first(rig, strata=["claims"])
    rig.strata.loaded = True
    web.watch(rig.arb, polls=1)

    asked = {a["pid"]: a for a in rig.arb.events.open_asks()}
    assert sorted(asked) == [4242, 9999], "one question for two games: one of them can never answer"
    assert {a["id"] for a in rig.arb.notify.asks} == {a["id"] for a in asked.values()}, \
        "a game playing with no question in front of it"
    assert (asked[4242].get("scope"), asked[9999].get("scope")) == ("pid4242", "pid9999")
    assert rig.arb.asks_for(policy.CLAIMS, "pid4242")["strata"]["id"] == asked[4242]["id"]
    assert rig.arb.asks_for(policy.CLAIMS, "pid9999")["strata"]["id"] == asked[9999]["id"]

    del procs[9999]                                   # Hades exits; Elden Ring keeps playing
    web.watch(rig.arb, polls=1)
    assert [(r["id"], r["closed_by"]) for r in rows(rig, "ask-end")] == \
        [(asked[9999]["id"], "claim-ended")], "the wrong game's question closed, or none did"
    assert [a["id"] for a in rig.arb.events.open_asks()] == [asked[4242]["id"]], \
        "the game still playing lost its question because the other one went away"
    assert rig.arb.answer_ask(asked[4242]["id"], policy.RELEASE)[0] == 200, \
        "the question of a game that is still running was not answerable"
    web.watch(rig.arb, polls=1)
    assert not rig.strata.loaded and "POST /unload" in rig.strata.calls, \
        "the answer Elden Ring was asked for did not free the card for Elden Ring"


def test_a_second_pressure_episode_is_asked_its_own_question(rig, monkeypatch):
    """The same identity on the other never-chosen requester: an episode is the row that opened it,
    not the card being short. `cooldown_s` can be shorter than `ask.timeout_s`, so the card can go
    short again while the previous episode's question is still counting down - and joining the new
    episode to that question is the same broken click as the second game inheriting the first
    game's: Unload now answers 200, the release walks past it because the episode it authorised is
    over, and another question shows up a poll later.

    So the episode ending closes the questions that episode was asking, and the new one is asked."""
    grower_pid = scene(rig, monkeypatch)
    grower = next(e for e in rig.gpu.extra if e["pid"] == grower_pid)
    ask_first(rig, strata=["pressure"])
    rig.arb.pressure_cfg["cooldown_s"] = 0            # next episode may start inside the timeout
    web.pressure_pass(rig.arb)
    first = rig.arb.events.open_ask("strata", policy.PRESSURE)
    assert first and (first.get("scope") or "").startswith("ep"),         "the question has no episode it belongs to"
    assert rig.strata.loaded, "an unanswered ask unloaded something"

    grower["mib"] = 2000                    # the app let go of the memory
    web.pressure_pass(rig.arb)
    assert rig.arb.events.episode_open() is None
    assert [(r["id"], r["closed_by"]) for r in rows(rig, "ask-end")] == \
        [(first["id"], "episode-ended")], "the episode ended and its question stayed open"
    assert rig.arb.answer_ask(first["id"], policy.RELEASE)[0] == 409, \
        "a closed episode's question can still be answered"

    grower["mib"] = 9000                    # the same card, short again
    samples(rig, free=1176, other=1000)
    web.pressure_pass(rig.arb)
    again = rig.arb.events.open_ask("strata", policy.PRESSURE)
    assert again and again["id"] != first["id"], "the new episode inherited the old one's question"
    assert again.get("scope") not in (None, "", first.get("scope"))
    assert rig.arb.answer_ask(again["id"], policy.RELEASE)[0] == 200
    web.pressure_pass(rig.arb)
    assert not rig.strata.loaded and "POST /unload" in rig.strata.calls, \
        "the answer to the second episode's question authorised nothing"


def test_a_fallback_the_log_wont_take_leaves_the_waiter_waiting(rig, monkeypatch, waiters):
    """When an ask's timeout runs out, the waiter applies its fallback - and the fallback is a row
    in the log, which means writing it can fail the way any write here fails: another writer holds
    the log and won't let go inside the bounded wait. That is not an answer, and treating it as one
    is worse than the crash this test used to get.

    The waiter used to ignore the write's outcome, then re-read an ask that was still unanswered and
    index its answer: `KeyError`, out of a gate hook, a guard's `--request` and a proxy's
    `Seat.acquire` alike - a request dying with a traceback because the log was busy, mid-handover.
    So a fallback that was not written leaves the question pending: the waiter keeps waiting and
    tries again on the next poll, still stopping at its own deadline, and an answer is never read
    out of a row that has none. Once the log comes back, the fallback is written - once - and the
    request that was waiting finishes on it."""
    monkeypatch.setattr(events, "LOCK_WAIT_S", 0.05)     # the test-scale bounded wait: fail fast
    # real seconds, unlike the sleeps: long enough to hold the log across the whole timeout
    rig.arb.ask.update({"timeout_s": 2, "fallback": policy.RELEASE})   # a question timed in
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = awaiting(rig, waiters, "strata", "comfy")

    stack = contextlib.ExitStack()
    stack.enter_context(rig.arb.events.turn(what="a writer that never lets go"))
    try:
        wait_for(lambda: None if rig.arb.ask_view(asked)["left_s"] > 0 else asked,
                 "the question's timeout running out while the log is held")
        time.sleep(0.4)                     # several polls, each one refused by the held log
        assert "error" not in box, f"the waiter died on a log it could not write: {box['error']!r}"
        assert th.is_alive(), "the request stopped waiting before it had an answer of any kind"
        assert rows(rig, "ask-answer") == [], "a fallback was recorded that was never written"
    finally:
        stack.close()

    answered = wait_for(lambda: rig.arb.events.ask_by_id(asked["id"]).get("answer")
                        and rig.arb.events.ask_by_id(asked["id"]),
                        "the fallback, once the log was free")
    assert (answered["answer"], answered["by"]) == (policy.RELEASE, "timeout")
    assert len([r for r in rows(rig, "ask-answer") if r["id"] == asked["id"]]) == 1,         "the retried fallback was written more than once"
    assert join(th, box) == 0, "the request did not finish on the fallback it was waiting for"
    assert not rig.strata.loaded and "POST /unload" in rig.strata.calls,         "nobody answered, and the fallback said release"


def test_a_question_the_log_wont_take_is_a_refusal_not_a_crash(rig, monkeypatch):
    """Opening a question is a write too. When another writer holds the log past its bounded wait,
    a gate, a `--request` or a `Seat.acquire` must come back with the refusal an unasked question
    gets - not an uncaught `Locked` out of the hook - and nothing may be unloaded for it."""
    monkeypatch.setattr(events, "LOCK_WAIT_S", 0.05)
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    with rig.arb.events.turn(what="a writer that never lets go"):
        th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
        code = join(th, box, within=12.0)
    assert "error" not in box, f"the request died on a log it could not write: {box['error']!r}"
    assert code == 1, "a question that was never asked let the load through"
    assert not rows(rig, "ask"), "a question was written to a log that wouldn't take it"
    assert rig.strata.loaded and "POST /unload" not in rig.strata.calls, \
        "nobody was asked, and a model was unloaded anyway"


def test_a_log_that_never_lets_go_stops_the_wait_rather_than_hanging_it(rig, monkeypatch,
                                                                        waiters):
    """The retry has a limit, because a log held by a writer that never lets go is not a question
    that will ever be answered. Without one, a waiting `--request` or `Seat.acquire` polls a log it
    cannot write forever, hung behind a wedged process, and the owner watching the dashboard sees
    nothing happen. So after `ask_write_tries` refused writes the waiter stops waiting on that
    question: the request is refused the way an unanswered one is refused, the workload stays loaded
    because no answer ever said otherwise, and the question is left open in the log - the next
    actor that can write it, or the owner's click, still closes it."""
    monkeypatch.setattr(events, "LOCK_WAIT_S", 0.05)
    # Long enough that the log is wedged below before the fallback is due: at 1 s a busy machine
    # sometimes got the fallback written first, and the test then failed on the answer it found.
    rig.arb.ask.update({"timeout_s": 4, "fallback": policy.RELEASE})
    rig.arb.ask_write_tries = 2
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = awaiting(rig, waiters, "strata", "comfy")

    stack = contextlib.ExitStack()
    stack.enter_context(rig.arb.events.turn(what="a writer that never lets go"))
    try:
        assert join(th, box, within=12.0) == 1, "the request hung on a log it could not write"
        assert "error" not in box, f"the waiter died on a log it could not write: {box['error']!r}"
    finally:
        stack.close()

    assert rig.arb.events.ask_by_id(asked["id"]).get("answer") is None,         "a fallback that was never written still decided something"
    assert rig.arb.open_asks(), "the wedged write closed the question the owner can still answer"
    assert rig.strata.loaded and "POST /unload" not in rig.strata.calls,         "an answer nobody wrote unloaded a model"


def test_a_game_starting_during_the_wait_refuses_the_request(rig, monkeypatch, waiters):
    """A request that waits for an answer waits for as long as `ask.timeout_s` says - two minutes of
    somebody being away from the keyboard - and a game can start inside that. The claim rule is about
    a model not going onto the card while a game owns it, so the claim is asked about again when the
    waiting ends: the request is refused on the sentence it would have got had it arrived a minute
    later, rather than putting a server's weights under the game that started meanwhile. The gate hook
    re-checks under its own lock; a guard's `--request` and a proxy's Seat wait outside any lock, so
    they owe the re-check themselves. What the owner agreed to unload is not unloaded by a refused
    request either: the claim's own release path frees the card for the game, and says so under the
    claim's name rather than this request's."""
    procs = claim_rule(rig, monkeypatch)
    pid, game = procs.popitem()                 # the game starts after the request begins waiting
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = awaiting(rig, waiters, "strata", "comfy")
    assert rig.arb.claim_hits() == [], "the game was claiming before it started"
    procs[pid] = game
    assert rig.arb.claim_hits(), "the fake game isn't in the process table"
    assert rig.arb.answer_ask(asked["id"], policy.RELEASE)[0] == 200
    assert join(th, box) == 1, "the request loaded ComfyUI on top of a game that had just started"
    assert "claimed by EldenRing.exe (Elden Ring)" in rows(rig, "switch")[-1]["error"]
    assert rig.comfy.vram == COMFY_CONTEXT_VRAM, "ComfyUI loaded its weights anyway"
    assert rig.strata.loaded, "the refusal unloaded something it was no longer asked to unload"


def test_a_gate_hook_waits_for_the_answer_instead_of_refusing_the_agent(rig, waiters):
    """The load is inside the hook, so the waiting has to be too: the point of an Ask me at a gate
    is that the owner gets asked rather than the agent getting a refusal. The release the answer
    agrees to then happens under the lock, in the same hook, as it always did."""
    ask_first(rig, comfy=["strata"])
    rig.comfy.vram = COMFY_MODEL_VRAM
    th, box = in_thread(lambda: rig.arb.gate("strata"))
    asked = awaiting(rig, waiters, "comfy", "strata")
    assert rig.comfy.vram == COMFY_MODEL_VRAM, "the gate unloaded the holder before asking"
    assert rig.arb.answer_ask(asked["id"], policy.RELEASE)[0] == 200
    assert join(th, box) is True
    assert rig.comfy.vram == 400 and not rows(rig, "policy-refused")


def test_a_gate_that_runs_out_of_its_own_time_leaves_the_question_open(rig, waiters):
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
    waiters.clear()           # the request that already gave up is not the one being waited on now
    th, box = in_thread(lambda: rig.arb.gate("strata"))
    second = awaiting(rig, waiters, "comfy", "strata")
    assert second["id"] == asked["id"] and len(rows(rig, "ask")) == 1
    rig.arb.answer_ask(asked["id"], policy.RELEASE)
    assert join(th, box) is True and rig.comfy.vram == 400


def test_a_proxy_waits_and_a_second_request_joins_the_question(rig, waiters):
    """The library's Seat is what a proxy in front of a server with no load hook runs: it waits on
    the same log, needs no channel back to anything, and stamps the workload as used because on
    that box nothing else knows a request arrived at all. And the queue behind it asks once: fifty
    requests waiting for one card are one question to the owner."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    seat = api.Seat(rig.arb)
    th, box = in_thread(lambda: seat.acquire("comfy"))
    asked = awaiting(rig, waiters, "strata", "comfy")
    assert rig.arb.last_used().get("comfy"), "the proxy didn't note its own request arriving"
    th2, box2 = in_thread(lambda: rig.arb.ask_before_request("comfy", source="gate"))
    wait_for(lambda: th.is_alive() and th2.is_alive() and waiters.count(asked["id"]) == 2,
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
    time.sleep(0.15)                 # the ask's own timeout, which is real time, has run out
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


def test_the_dashboard_shows_the_question_and_its_button_answers_it(served, rig, waiters):
    """The page is the other way in: `/api/status` carries the open question with its words and its
    countdown, and the button posts to the ask's own id - so the numbers an owner answered are the
    numbers they were shown, and an answer can't be applied to a question they never saw."""
    ask_first(rig, strata=["comfy"])
    rig.strata.loaded = True
    th, box = in_thread(lambda: rig.cli("switch", "comfy", "--request"))
    asked = awaiting(rig, waiters, "strata", "comfy")
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


def test_a_saved_policy_starts_the_asking_without_a_restart(served, rig, waiters):
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
    asked = awaiting(rig, waiters, "strata", "comfy")
    assert asked["source"] == "guard"
    rig.arb.answer_ask(asked["id"], policy.RELEASE)
    assert join(th, box) == 0 and not rig.strata.loaded
