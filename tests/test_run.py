"""Tickets for holds (GPU scheduling slice 2, chunk B): `Schedule.claim`, `vhotseat run` and
`hold --class` (take a ticket, wait for its turn, hold, finish), `vhotseat should-yield`, and the
fail-safe. The fake box from conftest; a real schedule file; `alive` decides which pids run."""

import json
import os
import sys
import time

import pytest

from vhotseat import schedule

ME = os.getpid()


@pytest.fixture
def q(rig):
    """The rig with a schedule attached. `rig.live` is the set of pids the schedule thinks run;
    this test process is in it (a run's ticket is this process's)."""
    rig.live = {ME}
    rig.arb.schedule = schedule.Schedule(rig.state / "schedule.db", schedule.cfg({}),
                                         alive=lambda pid: pid in rig.live)
    return rig


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def events(r, prefix: str) -> list[dict]:
    return [e for e in r.events() if e["event"].startswith(prefix)]


# --- Schedule.claim -------------------------------------------------------------------------------

def test_claim_starts_the_head_only_and_only_one_at_a_time(q):
    s = q.arb.schedule
    t1 = s.take("a", "batch", "comfy", pid=ME)
    t2 = s.take("b", "batch", "comfy", pid=ME)
    assert not s.claim(t2["id"])                        # not its turn: t1 is older
    assert s.claim(t1["id"])
    assert s.ticket(t1["id"])["state"] == "running"
    assert not s.claim(t2["id"])                        # t1 runs
    assert not s.claim(t1["id"])                        # already running: not claimed twice
    s.finish(t1["id"])
    assert s.claim(t2["id"])


def test_claim_never_starts_a_second_runner(q):
    """Even when a ticket is the head, it doesn't start while another ticket is running, however
    that one came to run (the update itself checks, so two racing claimers can't both start)."""
    s = q.arb.schedule
    t1 = s.take("a", "batch", "comfy", pid=ME)
    t2 = s.take("b", "agent", "strata", pid=ME)          # higher class: the head
    s.start(t1["id"])                                   # t1 got in first
    assert s.head()["id"] == t2["id"]
    assert not s.claim(t2["id"])


# --- run without a schedule -----------------------------------------------------------------------

def test_run_without_a_schedule_is_a_hold(rig):
    assert rig.arb.schedule is None
    assert rig.cli("run", "comfy", "--", *py("raise SystemExit(7)")) == 7
    assert [e["event"] for e in events(rig, "hold")] == ["hold", "hold-end"]
    assert events(rig, "ticket") == []


# --- run with a schedule --------------------------------------------------------------------------

def test_run_takes_a_ticket_holds_and_finishes(q, tmp_path):
    out = tmp_path / "env.txt"
    code = q.cli("run", "comfy", "--class", "batch", "--for", "10m", "--as", "lead-38", "--",
                 *py(f"import os,pathlib; pathlib.Path({str(out)!r}).write_text("
                     "os.environ.get('VHOTSEAT_TICKET','') + ' ' + os.environ.get('VHOTSEAT_HOLD',''));"
                     "raise SystemExit(3)"))
    assert code == 3                                     # the command's own exit code
    tid, held = out.read_text().split()
    assert held == "comfy"
    t = q.arb.schedule.ticket(int(tid))
    assert t["state"] == "done" and t["requester"] == "lead-38" and t["class"] == "batch"
    assert t["workload"] == "comfy" and t["est_s"] == 600 and t["pid"] == ME
    kinds = [e["event"] for e in q.events() if e["event"].startswith(("ticket", "hold"))]
    assert kinds == ["ticket", "hold", "ticket-start", "hold-end", "ticket-end"]   # started once the card is its
    end = events(q, "ticket-end")[-1]
    assert end["ticket"] == int(tid) and end["exit"] == 3 and end["state"] == "done"


def test_run_defaults_to_class_batch_and_names_its_requester(q, tmp_path):
    assert q.cli("run", "comfy", "--", *py("pass")) == 0
    t = q.arb.schedule.tickets(include_done=True)[-1]
    assert t["class"] == "batch" and t["requester"]       # a requester is always recorded


def test_run_refuses_an_unknown_class(q):
    assert q.cli("run", "comfy", "--class", "vip", "--", *py("pass")) == 2


def test_run_waits_for_its_turn_and_gives_up_at_the_queue_timeout(q, tmp_path, capsys):
    s = q.arb.schedule
    q.live.add(999)
    other = s.take("someone", "batch", "comfy", pid=999)
    s.start(other["id"])                                 # somebody else's ticket is running
    ran = tmp_path / "ran"
    started = time.monotonic()
    code = q.cli("run", "comfy", "--queue-timeout", "5", "--",       # 5 s at SCALE 0.1: 0.5 s
                 *py(f"import pathlib; pathlib.Path({str(ran)!r}).touch()"))
    assert code == 1 and not ran.exists()
    assert time.monotonic() - started < 10
    mine = s.tickets(include_done=True)[-1]
    assert mine["state"] == "expired"
    err = capsys.readouterr().err
    assert err.count("waiting for the card") == 1            # said once, not every poll
    assert f"ticket {mine['id']}" in err
    end = events(q, "ticket-end")[-1]
    assert end["ticket"] == mine["id"] and end["state"] == "expired"


def test_run_starts_once_the_runner_ahead_has_gone(q, tmp_path):
    import threading
    s = q.arb.schedule
    q.live.add(999)
    other = s.take("someone", "batch", "comfy", pid=999)
    s.start(other["id"])
    threading.Timer(0.5, lambda: q.live.discard(999)).start()     # the runner ahead crashes
    ran = tmp_path / "ran"
    code = q.cli("run", "comfy", "--queue-timeout", "100", "--",
                 *py(f"import pathlib; pathlib.Path({str(ran)!r}).touch()"))
    assert code == 0 and ran.exists()
    assert s.ticket(other["id"])["state"] == "expired"


def test_run_waits_out_a_booking_that_excludes_it(q, tmp_path):
    now = time.time()
    q.arb.schedule.add_booking("Peter's day", now - 60, now + 3600, cls="peter")
    assert q.cli("run", "comfy", "--class", "batch", "--queue-timeout", "3", "--", *py("pass")) == 1


def test_hold_with_a_class_is_a_run(q):
    assert q.cli("hold", "comfy", "--class", "batch", "--for", "5m", "--", *py("pass")) == 0
    t = q.arb.schedule.tickets(include_done=True)[-1]
    assert t["class"] == "batch" and t["est_s"] == 300 and t["state"] == "done"


def test_plain_hold_takes_no_ticket(q):
    """A typed `hold` without --class is an order, as before: no queue."""
    assert q.cli("hold", "comfy", "--", *py("pass")) == 0
    assert q.arb.schedule.tickets(include_done=True) == []


def test_run_falls_back_to_a_hold_when_the_schedule_is_unreadable(q, capsys):
    q.arb.schedule.path = q.state                      # a directory: sqlite can't open it
    assert q.cli("run", "comfy", "--", *py("raise SystemExit(5)")) == 5
    assert "schedule unreadable" in capsys.readouterr().err
    assert [e["event"] for e in events(q, "hold")] == ["hold", "hold-end"]


def test_run_inside_its_own_hold_just_runs(q, monkeypatch):
    monkeypatch.setenv("VHOTSEAT_HOLD", "comfy")
    assert q.cli("run", "comfy", "--", *py("raise SystemExit(4)")) == 4
    assert q.arb.schedule.tickets(include_done=True) == []


# --- should-yield ---------------------------------------------------------------------------------

def test_should_yield(q, capsys, monkeypatch):
    s = q.arb.schedule
    clock = {"t": time.time()}
    s.now = lambda: clock["t"]
    t = s.take("a", "batch", "comfy", pid=ME)
    s.start(t["id"])
    assert q.cli("should-yield", "--ticket", str(t["id"])) == 1          # carry on
    clock["t"] += 1300                                                  # past batch's 1200 s
    s.take("b", "batch", "strata", pid=ME)
    capsys.readouterr()
    assert q.cli("should-yield", "--ticket", str(t["id"])) == 0          # yield
    assert "1 waiting" in capsys.readouterr().out
    monkeypatch.setenv("VHOTSEAT_TICKET", str(t["id"]))
    assert q.cli("should-yield") == 0                                   # the ticket from the env
    assert q.cli("should-yield", "--json") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["yield"] is True


def test_should_yield_without_a_ticket_or_schedule(rig, q, monkeypatch):
    monkeypatch.delenv("VHOTSEAT_TICKET", raising=False)
    assert q.cli("should-yield") == 2                                   # no ticket named
    q.arb.schedule = None
    assert q.cli("should-yield", "--ticket", "1") == 1                  # no schedule: never yield


def test_a_schedule_that_breaks_while_waiting_falls_back_to_a_hold(q, capsys, monkeypatch):
    s = q.arb.schedule

    def broken(*_a, **_k):
        raise schedule.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(s, "claim", broken)
    assert q.cli("run", "comfy", "--", *py("raise SystemExit(6)")) == 6
    assert "schedule unreadable" in capsys.readouterr().err
    assert s.tickets(include_done=True)[-1]["state"] == "expired"


def test_the_commands_exit_code_survives_a_failed_close(q, capsys, monkeypatch):
    s = q.arb.schedule
    real = s.finish

    def finish(tid, state="done"):
        if state == "done":
            raise schedule.sqlite3.OperationalError("disk I/O error")
        return real(tid, state)
    monkeypatch.setattr(s, "finish", finish)
    assert q.cli("run", "comfy", "--", *py("raise SystemExit(9)")) == 9
    assert "closing it failed" in capsys.readouterr().err
    assert s.tickets(include_done=True)[-1]["state"] == "expired"     # never left running
