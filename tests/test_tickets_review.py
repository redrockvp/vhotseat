"""Slice 2 review fixes (an independent review of feat/gpu-tickets, 2026-11-18): process identity
against pid reuse, a process we may not inspect is not dead, overdue counts only waiters that could
start, run()'s fail-safe on every schedule call, the max-hold clock starts when the card is
obtained, ticket-end on every path, no spurious schedule-override inside a ticket's hold,
$VHOTSEAT_AS, and comfy-render's `--hold=FILE`, numbering and one JSON document across rounds,
and jobs a timeout kept from being submitted."""

import datetime
import importlib
import json
import os
import sys
import time

import pytest
from fakes import FakeComfy

from vhotseat import schedule

ME = os.getpid()


# --- schedule: liveness ---------------------------------------------------------------------------

@pytest.fixture
def sched(tmp_path):
    clock = {"t": datetime.datetime(2026, 11, 16, 12).timestamp()}
    ident = {}
    s = schedule.Schedule(tmp_path / "s.db", schedule.cfg({}), now=lambda: clock["t"],
                          alive=lambda pid: pid in ident, identity=lambda pid: ident.get(pid))
    s.clock, s.ident = clock, ident
    return s


def test_a_reused_pid_does_not_keep_a_dead_ticket_alive(sched):
    sched.ident[50] = 1000.0                               # the runner, started at t=1000
    t = sched.take("a", "batch", "comfy", pid=50)
    sched.start(t["id"])
    sched.ident[50] = 2000.0                               # it died; Windows gave 50 to another process
    t2 = sched.take("b", "batch", "comfy", pid=None)
    assert sched.ticket(t["id"])["state"] == "running"
    assert sched.my_turn(t2["id"])                         # the dead runner no longer blocks the queue
    assert sched.ticket(t["id"])["state"] == "expired"


def test_without_an_identity_the_pid_alone_decides(sched):
    t = sched.take("a", "batch", "comfy", pid=60)          # identity unknown when it was taken
    sched.ident[60] = 5.0                                  # alive now (and has some identity)
    assert sched.ticket(t["id"])["state"] == "waiting" and sched.order()[0]["id"] == t["id"]


def test_real_process_checks():
    assert schedule._pid_alive(ME)
    assert not schedule._pid_alive(None)
    a, b = schedule._pid_identity(ME), schedule._pid_identity(ME)
    assert a == b                                          # stable for a running process
    if os.name == "nt" or sys.platform.startswith("linux"):
        assert a is not None
    if os.name == "nt":
        # The System process: a normal user may not query it. Not being allowed to look is not
        # the same as the process being gone.
        assert schedule._pid_alive(4)


def test_overdue_ignores_waiters_a_booking_keeps_off(sched):
    t = sched.take("a", "batch", "comfy", pid=None)
    sched.start(t["id"])
    sched.clock["t"] += 1300
    now = sched.clock["t"]
    sched.add_booking("schoolmap", now - 60, now + 3600, workloads=["comfy"])   # strata can't start
    sched.take("b", "batch", "strata", pid=None)
    sched.beat(t["id"])
    assert sched.overdue(t["id"]) is None                  # nobody who could take the card waits


# --- run() ---------------------------------------------------------------------------------------

@pytest.fixture
def q(rig):
    rig.live = {ME}
    rig.arb.schedule = schedule.Schedule(rig.state / "schedule.db", schedule.cfg({}),
                                         alive=lambda pid: pid in rig.live)
    return rig


def py(code):
    return [sys.executable, "-c", code]


def ends(r):
    return [e for e in r.events() if e["event"] == "ticket-end"]


def test_run_survives_a_schedule_that_breaks_after_the_first_claim(q, capsys, monkeypatch):
    s = q.arb.schedule
    q.live.add(999)
    s.start(s.take("other", "batch", "comfy", pid=999)["id"])       # so the first claim fails

    def broken(*a, **k):
        raise schedule.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(s, "queue_view", broken)
    assert q.cli("run", "comfy", "--", *py("raise SystemExit(6)")) == 6
    assert "schedule unreadable" in capsys.readouterr().err
    assert ends(q)[-1]["state"] == "expired"                # the fallback is recorded too


def test_queue_timeout_with_a_broken_close_still_exits_1(q, monkeypatch):
    s = q.arb.schedule
    q.live.add(999)
    s.start(s.take("other", "batch", "comfy", pid=999)["id"])
    real = s.finish

    def finish(tid, state="done"):
        if state == "expired" and tid != 1:
            raise schedule.sqlite3.OperationalError("disk I/O error")
        return real(tid, state)
    monkeypatch.setattr(s, "finish", finish)
    assert q.cli("run", "comfy", "--queue-timeout", "2", "--", *py("pass")) == 1


def test_the_max_hold_clock_starts_when_the_card_is_obtained(q, monkeypatch):
    s = q.arb.schedule
    clock = {"t": time.time()}
    s.now = lambda: clock["t"]
    real_take = q.arb.lock.take

    def slow_take(*a, **k):
        clock["t"] += 5000                                 # a typed hold had the lock for 83 min
        return real_take(*a, **k)
    monkeypatch.setattr(q.arb.lock, "take", slow_take)
    assert q.cli("run", "comfy", "--", *py("pass")) == 0
    t = s.tickets(include_done=True)[-1]
    assert t["started"] == clock["t"]                      # not the claim time 5000 s earlier
    start = [e for e in q.events() if e["event"] == "ticket-start"][-1]
    hold = [e for e in q.events() if e["event"] == "hold"][-1]
    assert q.events().index(start) > q.events().index(hold)   # recorded once the card is its


def test_an_error_in_the_hold_records_ticket_end(q, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(q.arb, "hold", boom)
    with pytest.raises(RuntimeError):
        q.arb.run("comfy", py("pass"), 0)
    assert ends(q)[-1]["state"] == "expired"
    assert q.arb.schedule.tickets(include_done=True)[-1]["state"] == "expired"


def test_a_tickets_hold_records_no_schedule_override(q):
    """The ticket was admitted by its own class: the hold under it is not an order overriding
    the booking (a plain typed hold still is)."""
    now = time.time()
    q.arb.schedule.add_booking("Peter", now - 60, now + 3600, cls="peter")
    assert q.cli("run", "strata", "--class", "peter", "--", *py("pass")) == 0
    assert not [e for e in q.events() if e["event"] == "schedule-override"]


def test_vhotseat_as_names_the_requester(q, monkeypatch):
    monkeypatch.setenv("VHOTSEAT_AS", "lead-38")
    assert q.cli("run", "comfy", "--", *py("pass")) == 0
    assert q.arb.schedule.tickets(include_done=True)[-1]["requester"] == "lead-38"


# --- comfy-render --------------------------------------------------------------------------------

@pytest.fixture
def cr(monkeypatch):
    mod = importlib.reload(importlib.import_module("vhotseat.kit.comfy_render"))
    comfy = FakeComfy()
    monkeypatch.setattr(mod, "COMFY_URL", comfy.url)
    monkeypatch.setattr(mod, "COMFY_DIR", None)
    monkeypatch.setattr(mod, "POLL_S", 0.05)
    for name in ("VHOTSEAT_TICKET", "COMFY_RENDER_REST", "COMFY_RENDER_JOBS", "COMFY_RENDER_FIRST",
                 "COMFY_RENDER_TOTAL", "COMFY_RENDER_RESULTS"):
        monkeypatch.delenv(name, raising=False)
    mod.fake = comfy
    yield mod
    comfy.close()


def save(seed=1):
    return {"1": {"class_type": "KSampler", "inputs": {"seed": seed}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}}


def write(path, data):
    path.write_text(json.dumps(data))
    return str(path)


def _rounds(cr, monkeypatch, rounds):
    """Stub vhotseat: report which jobs each round runs; rounds 1..n-1 yield one job back."""
    monkeypatch.setenv("VHOTSEAT_BIN", "vhotseat-test")
    seen = []

    def call(argv, env=None, **kw):
        inner = argv[argv.index("--") + 1:]
        jobs = env.get("COMFY_RENDER_JOBS") or next(
            a.split("=", 1)[-1] for a in inner[3:] if a.split("=", 1)[-1].endswith(".json"))
        seen.append(json.loads(open(jobs).read()))
        if len(seen) < rounds:
            rest = env["COMFY_RENDER_REST"]
            open(rest, "w").write(json.dumps([save(100 + len(seen))]))
            open(rest + ".code", "w").write("0")
            return 5
        return 0
    monkeypatch.setattr(cr.subprocess, "call", call)
    return seen


def test_hold_equals_file_requeues_the_rest_and_ends(cr, tmp_path, monkeypatch):
    seen = _rounds(cr, monkeypatch, rounds=3)
    assert cr.main([f"--hold={write(tmp_path / 'jobs.json', [save(1)])}"]) == 0
    assert seen == [[save(1)], [save(101)], [save(102)]]


def test_inner_rounds_number_jobs_across_the_batch(cr, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VHOTSEAT_HOLD", "comfy")
    monkeypatch.setenv("VHOTSEAT_TICKET", "7")
    monkeypatch.setenv("COMFY_RENDER_REST", str(tmp_path / "rest.json"))
    monkeypatch.setenv("COMFY_RENDER_FIRST", "4")          # this round is jobs 4-5 of 5
    monkeypatch.setenv("COMFY_RENDER_TOTAL", "5")
    monkeypatch.setattr(cr, "should_yield", lambda: False)
    assert cr.main([write(tmp_path / "jobs.json", [save(4), save(5)]), "--hold"]) == 0
    out = capsys.readouterr().out
    assert "job 4/5 ok" in out and "job 5/5 ok" in out


def test_json_rounds_add_up_to_one_document(cr, tmp_path, monkeypatch, capsys):
    """Each inner round leaves its results in $COMFY_RENDER_RESULTS; the outer prints one array."""
    monkeypatch.setenv("VHOTSEAT_BIN", "vhotseat-test")
    calls = []

    def call(argv, env=None, **kw):
        calls.append(env)
        first = int(env.get("COMFY_RENDER_FIRST", "1"))
        open(env["COMFY_RENDER_RESULTS"], "w").write(json.dumps([{"job": first, "status": "done", "files": []}]))
        if len(calls) == 1:
            open(env["COMFY_RENDER_REST"], "w").write(json.dumps([save(2)]))
            open(env["COMFY_RENDER_REST"] + ".code", "w").write("0")
            return 5
        return 0
    monkeypatch.setattr(cr.subprocess, "call", call)
    assert cr.main([write(tmp_path / "jobs.json", [save(1), save(2)]), "--hold", "--json"]) == 0
    assert calls[1]["COMFY_RENDER_FIRST"] == "2" and calls[1]["COMFY_RENDER_TOTAL"] == "2"
    out = capsys.readouterr().out
    assert [r["job"] for r in json.loads(out[out.index("["):])] == [1, 2]


def test_inner_json_writes_results_for_the_outer(cr, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VHOTSEAT_HOLD", "comfy")
    monkeypatch.setenv("VHOTSEAT_TICKET", "7")
    monkeypatch.setenv("COMFY_RENDER_REST", str(tmp_path / "rest.json"))
    res = tmp_path / "res.json"
    monkeypatch.setenv("COMFY_RENDER_RESULTS", str(res))
    monkeypatch.setattr(cr, "should_yield", lambda: False)
    assert cr.main([write(tmp_path / "jobs.json", [save(1), save(2)]), "--hold", "--json"]) == 0
    assert [r["job"] for r in json.loads(res.read_text())] == [1, 2]
    assert "[" not in capsys.readouterr().out               # the outer prints the document


def test_a_timeout_reports_the_jobs_it_kept_from_being_submitted(cr, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VHOTSEAT_HOLD", "comfy")
    monkeypatch.setenv("VHOTSEAT_TICKET", "7")
    monkeypatch.setenv("COMFY_RENDER_REST", str(tmp_path / "rest.json"))
    monkeypatch.setattr(cr, "should_yield", lambda: False)
    cr.fake.slow_s = 30
    slow = {"1": {"class_type": "Slow", "inputs": {}}, "2": {"class_type": "SaveImage", "inputs": {}}}
    jobs = write(tmp_path / "jobs.json", [slow, save(2), save(3)])
    assert cr.main([jobs, "--hold", "--yield-every", "1", "--timeout", "0.5", "--json"]) == cr.EXIT_TIMEOUT
    out = capsys.readouterr().out
    doc = json.loads(out[out.index("["):])
    assert [r["job"] for r in doc] == [1, 2, 3]
    assert [r["status"] for r in doc[1:]] == ["not submitted", "not submitted"]
