"""The schedule store on its own (schedule.py): bookings, recurrence, pausing, admission, `when`,
tickets' liveness, and the `schedule:` config block. Fake clock; a temp SQLite file."""

import datetime

import pytest

from vhotseat import config, schedule
from vhotseat.config import ConfigError

# Monday 2026-11-16, local time.
MON = datetime.datetime(2026, 11, 16, 0, 0)


def at(day: int, hh: int, mm: int = 0) -> float:
    """`day` days after Monday 16 Nov, at hh:mm local."""
    return (MON + datetime.timedelta(days=day, hours=hh, minutes=mm)).timestamp()


@pytest.fixture
def sched(tmp_path):
    clock = {"t": at(0, 12)}
    s = schedule.Schedule(tmp_path / "schedule.db", schedule.cfg({"default_class": {"comfy": "peter"}}),
                          now=lambda: clock["t"], alive=lambda pid: pid in (1, 2))
    s.clock = clock
    return s


def test_no_block_means_no_schedule():
    assert schedule.cfg(None) is None


def test_defaults_and_untagged_classes(sched):
    assert sched.classes == ["peter", "booked", "agent", "batch", "background"]
    assert sched.class_of("comfy") == "peter"      # Peter's decision 4: untagged ComfyUI jobs are his
    assert sched.class_of("strata") == "agent"
    assert sched.max_hold_s("batch") == 1200
    assert sched.rank("peter") < sched.rank("agent") < sched.rank("background")


def test_peters_day_keeps_agents_off_but_not_peter(sched):
    sched.add_booking("Peter's day", at(0, 8), at(0, 18), cls="peter", every="weekdays")
    r = sched.admit("strata")
    assert not r["ok"] and "18:00 for Peter" in r["reason"] and r["until"] == at(0, 18)
    assert sched.admit("comfy")["ok"]                       # class peter
    assert sched.admit("strata", cls="peter")["ok"]
    sched.clock["t"] = at(0, 19)
    assert sched.admit("strata")["ok"]


def test_weekdays_skip_the_weekend_and_keep_wall_clock(sched):
    b = sched.add_booking("Peter's day", at(0, 8), at(0, 18), cls="peter", every="weekdays")
    wins = sched.windows(b, at(0, 0), at(7, 0))
    assert len(wins) == 5
    assert all(datetime.datetime.fromtimestamp(s).hour == 8 for s, _ in wins)
    sched.clock["t"] = at(5, 12)                             # Saturday noon
    assert sched.admit("strata")["ok"]


def test_overnight_booking_for_a_workload(sched):
    sched.add_booking("schoolmap", at(0, 22), at(1, 1), workloads=["comfy"], note="extraction")
    sched.clock["t"] = at(0, 23)
    assert sched.admit("comfy", cls="batch")["ok"]           # the booked workload runs
    r = sched.admit("strata")
    assert not r["ok"] and "schoolmap" in r["reason"] and "extraction" in r["reason"]
    sched.clock["t"] = at(1, 2)
    assert sched.admit("strata")["ok"]


def test_a_planned_run_cant_go_over_the_owners_time(sched):
    sched.add_booking("Peter's day", at(0, 8), at(0, 18), cls="peter", every="daily")
    with pytest.raises(schedule.ScheduleError, match="owner's time"):
        sched.add_booking("schoolmap", at(1, 17), at(1, 19), workloads=["comfy"])
    sched.add_booking("schoolmap", at(1, 18), at(1, 20), workloads=["comfy"])   # touching is fine


def test_bad_bookings_are_refused(sched):
    for kw, msg in [({"cls": "agent"}, "class"), ({"every": "hourly"}, "every"), ({"mode": "soft"}, "mode")]:
        with pytest.raises(schedule.ScheduleError, match=msg):
            sched.add_booking("x", at(0, 13), at(0, 14), **kw)
    with pytest.raises(schedule.ScheduleError, match="end after"):
        sched.add_booking("x", at(0, 14), at(0, 13))
    with pytest.raises(schedule.ScheduleError, match="longer than a day"):
        sched.add_booking("x", at(0, 13), at(1, 14), every="daily")


def test_pause_switches_one_window_off(sched):
    b = sched.add_booking("Peter's day", at(0, 8), at(0, 18), cls="peter", every="daily")
    assert not sched.admit("strata")["ok"]
    sched.pause(b["id"])                                     # "the GPU is free today"
    assert sched.admit("strata")["ok"]
    sched.clock["t"] = at(1, 12)                             # tomorrow it's back
    assert not sched.admit("strata")["ok"]
    sched.pause(b["id"], until=at(1, 14))
    assert sched.admit("strata")["ok"]
    sched.clock["t"] = at(1, 15)
    assert not sched.admit("strata")["ok"]
    sched.pause(b["id"])
    sched.resume(b["id"])
    assert not sched.admit("strata")["ok"]


def test_when_looks_ahead_past_bookings(sched):
    sched.add_booking("Peter's day", at(0, 8), at(0, 18), cls="peter", every="weekdays")
    sched.clock["t"] = at(0, 7)
    w = sched.when("strata", duration_s=1800)
    assert w["now"] and w["start"] == at(0, 7)
    w = sched.when("strata", duration_s=7200)                # 07:00 + 2 h runs into 08:00
    assert not w["now"] and w["start"] == at(0, 18) and "Peter's day" in w["reason"]
    assert sched.when("strata", cls="peter", duration_s=7200)["now"]
    sched.add_booking("schoolmap", at(0, 18), at(0, 21), workloads=["comfy"])
    w = sched.when("strata", duration_s=7200)                # chained: 18:00-21:00 is taken too
    assert w["start"] == at(0, 21)
    assert sched.when("comfy", cls="batch", duration_s=7200)["start"] == at(0, 18)   # the booked run


def test_removed_booking_is_gone(sched):
    b = sched.add_booking("x", at(0, 11), at(0, 13), cls="peter")
    assert not sched.admit("strata")["ok"]
    sched.remove_booking(b["id"])
    assert sched.admit("strata")["ok"]
    with pytest.raises(schedule.ScheduleError):
        sched.remove_booking(b["id"])


def test_tickets_expire_with_their_process(sched):
    with sched._db() as con:
        for pid, cls, st in ((1, "batch", "waiting"), (2, "agent", "waiting"), (99, "peter", "waiting"),
                             (1, "background", "running")):
            con.execute("INSERT INTO ticket (requester, class, workload, pid, state, created, heartbeat) "
                        "VALUES (?,?,?,?,?,?,?)", (f"r{pid}", cls, "comfy", pid, st, sched.now(), sched.now()))
            sched.clock["t"] += 1
    q = sched.queue_view()
    assert [t["class"] for t in q] == ["background", "agent", "batch"]   # running first, then by class
    assert all(t["pid"] != 99 for t in q)                                 # dead process: expired
    assert [t["state"] for t in sched.tickets(include_done=True) if t["pid"] == 99] == ["expired"]


def test_parse_time(sched):
    now = at(0, 12)
    assert schedule.parse_time("22:00", now) == at(0, 22)
    assert schedule.parse_time("2026-11-17 08:30", now) == at(1, 8, 30)
    with pytest.raises(ValueError):
        schedule.parse_time("25:00", now)
    with pytest.raises(ValueError):
        schedule.parse_time("soon", now)


def _raw(sc):
    return {"workloads": {"strata": {"driver": "http", "url": "http://127.0.0.1:1"}}, "schedule": sc}


def test_config_accepts_a_schedule_block():
    config._schedule({"classes": schedule.DEFAULT_CLASSES, "default_class": {"strata": "agent"},
                      "affinity_s": 300}, {"strata"})


@pytest.mark.parametrize("sc,msg", [
    ({"nope": 1}, "unknown"),
    ({"classes": [{"name": "booked"}, {"name": "peter"}]}, "highest"),
    ({"classes": [{"name": "peter"}, {"name": "agent"}]}, "booked"),
    ({"classes": [{"name": "peter"}, {"name": "booked"}, {"name": "peter"}]}, "twice"),
    ({"classes": [{"name": "peter"}, {"name": "booked", "max_hold_s": 0}]}, "max_hold_s"),
    ({"default_class": {"comfy": "peter"}}, "no such workload"),
    ({"default_class": {"strata": "vip"}}, "no class"),
    ({"affinity_s": -1}, "affinity_s"),
])
def test_config_refuses_a_bad_schedule_block(sc, msg):
    with pytest.raises(ConfigError, match=msg):
        config._schedule(sc, {"strata"})
