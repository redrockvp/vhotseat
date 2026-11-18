import datetime
import sys

import pytest

from vhotseat import schedule
from vhotseat.api import Seat
from vhotseat.errors import ScheduleRefused


@pytest.fixture
def booked(rig):
    now = datetime.datetime.now().replace(second=0, microsecond=0).timestamp()
    rig.arb.schedule = schedule.Schedule(rig.state / "schedule.db", schedule.cfg({}), now=lambda: now)
    row = rig.arb.schedule.add_booking("owner", now - 60, now + 3600, cls="peter")
    return rig, row, now


def test_gate_and_seat(booked):
    r, row, now = booked
    assert not r.arb.gate("strata")
    assert r.events()[-1]["booking"] == row["id"]
    with pytest.raises(ScheduleRefused) as exc:
        Seat(r.arb).acquire("strata")
    assert exc.value.until == now + 3600
    r.strata.loaded = True
    assert r.arb.gate("strata")
    r.strata.loaded = False
    with r.arb._take("strata", skip_if_held=False):
        assert r.arb.gate("strata")
    r.arb.schedule.now = lambda: now + 4000
    assert r.arb.gate("strata")


def test_switch_hold_and_none(booked):
    r, row, now = booked
    result = r.arb.switch("strata", 0, request=True)
    assert not result["ok"] and result["until"] == now + 3600
    assert isinstance(result["exception"], ScheduleRefused)
    assert r.arb.switch("strata", 0)["ok"]
    assert any(e["event"] == "schedule-override" and e["booking"] == row["id"] for e in r.events())
    assert r.arb.switch("strata", 0, request=True)["ok"]
    assert r.arb.switch("none", 0)["ok"]
    assert r.arb.hold("strata", [sys.executable, "-c", "pass"], 0) == 0
    assert sum(e["event"] == "schedule-override" for e in r.events()) == 2


def test_broken_schedule_and_why(booked, capsys):
    r, _, now = booked
    assert r.arb.why_all()["strata"]["would"] == "blocked"
    r.arb.schedule.add_booking("later", now + 4000, now + 5000, cls="peter")
    assert "starts at" in " ".join(r.arb.why_all()["strata"]["notes"])
    r.strata.loaded = True
    assert "keeps what it has" in " ".join(r.arb.why_all()["strata"]["notes"])
    r.strata.loaded = False
    r.arb.schedule.path = r.state
    assert r.arb.gate("strata")
    assert capsys.readouterr().err.count("schedule unreadable:") == 1
    assert r.arb.switch("strata", 0, request=True)["ok"]
    assert capsys.readouterr().err.count("schedule unreadable:") == 1


def test_cli(booked, capsys):
    r, row, now = booked
    assert r.cli("when", "strata") == 1
    assert r.cli("when", "strata", "--class", "peter") == 0
    assert r.cli("when", "strata", "--class", "unknown") == 2
    assert r.cli("book", "other", "22:00", "01:00", "--workload", "unknown") == 2
    assert r.cli("bookings", "--json") == 0
    assert r.cli("queue", "--json") == 0
    assert r.cli("pause", str(row["id"])) == 0
    assert r.cli("resume", str(row["id"])) == 0
    assert r.cli("unbook", str(row["id"])) == 0
    assert r.cli("book", "night", "22:00", "01:00") == 0
    b = r.arb.schedule.bookings()[0]
    assert b["end"] > b["start"]
    r.arb.schedule = None
    for cmd in (("bookings",), ("queue",), ("when", "strata")):
        assert r.cli(*cmd) == 0
    for cmd in (("book", "a", "22:00", "01:00"), ("unbook", "1"), ("pause", "1"), ("resume", "1")):
        assert r.cli(*cmd) == 2


def test_build_makes_a_schedule_only_with_the_block(rig):
    from vhotseat import setup
    assert rig.arb.schedule is None                       # config.example.yaml keeps it commented out
    arb = setup.build(dict(rig.cfg, schedule={}), gpu=rig.gpu)
    assert arb.schedule is not None and arb.schedule.path == rig.state / "schedule.db"


def test_cli_book_details(booked, capsys):
    r, row, now = booked
    r.cli("unbook", str(row["id"]))
    assert r.cli("bookings") == 0
    assert "nothing booked" in capsys.readouterr().out
    # An HH:MM end before the start is the next morning.
    assert r.cli("book", "night", "22:00", "01:00", "--workload", "comfy", "--note", "bake") == 0
    b = r.arb.schedule.bookings()[-1]
    s, e = datetime.datetime.fromtimestamp(b["start"]), datetime.datetime.fromtimestamp(b["end"])
    assert (s.hour, e.hour) == (22, 1) and e.date() == s.date() + datetime.timedelta(days=1)
    assert b["workloads"] == ["comfy"] and b["note"] == "bake"
    assert any(x["event"] == "booking" and x["booking"] == b["id"] for x in r.events())
    capsys.readouterr()
    assert r.cli("bookings") == 0
    assert "night" in capsys.readouterr().out
    # A planned run can't go over the owner's time: refused, with the clash named.
    s0 = datetime.datetime.fromtimestamp(now)
    day = (s0 + datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    assert r.cli("book", "peter", f"{day} 08:00", f"{day} 18:00", "--class", "peter") == 0
    capsys.readouterr()
    assert r.cli("book", "lead", f"{day} 17:00", f"{day} 19:00") == 1
    assert "owner's time" in capsys.readouterr().err
    assert r.cli("unbook", "999") == 1                    # no such booking
    # A one-off that has already ended is refused as bad input.
    past = (s0 - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    assert r.cli("book", "x", f"{past} 08:00", f"{past} 09:00") == 2


def test_cli_unreadable_schedule(booked, capsys):
    r, _, _ = booked
    r.arb.schedule.path = r.state                         # a directory: sqlite can't open it
    capsys.readouterr()
    assert r.cli("bookings") == 1
    assert "schedule unreadable" in capsys.readouterr().err


def test_a_held_scripts_jobs_get_past_their_guard(rig):
    """A typed `hold comfy` is an order: the jobs its script queues get past the guard's
    `switch comfy --request` even while a booking keeps comfy off (a lazy workload reads "none"
    under its hold, so the mode alone doesn't show it)."""
    import threading
    import time
    now = time.time()
    rig.arb.schedule = schedule.Schedule(rig.state / "schedule.db", schedule.cfg({}))
    rig.arb.schedule.add_booking("lead", now - 60, now + 3600, workloads=["strata"])
    assert not rig.arb.switch("comfy", 0, load=False, request=True)["ok"]   # refused outside a hold
    out = {}

    def guard():
        deadline = time.monotonic() + 20
        while rig.arb.lock.holder() != "hold comfy" and time.monotonic() < deadline:
            time.sleep(0.05)
        out["holder"] = rig.arb.lock.holder()
        out["res"] = rig.arb.switch("comfy", 0, load=False, request=True)

    t = threading.Thread(target=guard)
    t.start()
    assert rig.arb.hold("comfy", [sys.executable, "-c", "import time; time.sleep(3)"], 0) == 0
    t.join()
    assert out["holder"] == "hold comfy" and out["res"]["ok"], out


def test_untagged_comfyui_jobs_are_the_owners(rig):
    """Decision 4 as a default: a ComfyUI-driver workload's untagged jobs are class peter, so the
    owner's own booking doesn't refuse the web page's jobs; default_class can still say otherwise."""
    from vhotseat import setup
    arb = setup.build(dict(rig.cfg, schedule={}), gpu=rig.gpu)
    assert arb.schedule.class_of("comfy") == "peter" and arb.schedule.class_of("strata") == "agent"
    arb = setup.build(dict(rig.cfg, schedule={"default_class": {"comfy": "batch"}}), gpu=rig.gpu)
    assert arb.schedule.class_of("comfy") == "batch"


def test_cli_time_edges(booked, capsys):
    r, row, _ = booked
    r.cli("unbook", str(row["id"]))
    late = datetime.datetime.now().replace(hour=23, minute=0, second=0, microsecond=0).timestamp()
    r.arb.schedule.now = lambda: late
    # At 23:00, `01:00 03:00` means tonight, not this morning.
    assert r.cli("book", "night", "01:00", "03:00") == 0
    b = r.arb.schedule.bookings()[-1]
    assert b["start"] > late and datetime.datetime.fromtimestamp(b["start"]).hour == 1
    # A pause until a time that has passed changes nothing: refused.
    assert r.cli("pause", str(b["id"]), "--until", "08:00") == 2
    assert r.cli("pause", str(b["id"]), "--until", "23:30") == 0


def test_a_daily_run_cant_go_over_a_far_owner_booking(sched_only):
    s, at = sched_only
    s.add_booking("Peter", at(20, 10), at(20, 12), cls="peter")             # a one-off 20 days out
    with pytest.raises(schedule.ScheduleError, match="owner's time"):
        s.add_booking("lead", at(0, 11), at(0, 13), every="daily")


@pytest.fixture
def sched_only(tmp_path):
    base = datetime.datetime(2026, 11, 16)

    def at(day, hh):
        return (base + datetime.timedelta(days=day, hours=hh)).timestamp()
    return schedule.Schedule(tmp_path / "s.db", schedule.cfg({}), now=lambda: at(0, 9)), at


def test_no_schedule_json_output(rig, capsys):
    import json
    assert rig.cli("bookings", "--json") == 0 and json.loads(capsys.readouterr().out) == []
    assert rig.cli("when", "strata", "--json") == 0 and json.loads(capsys.readouterr().out)["now"] is True
