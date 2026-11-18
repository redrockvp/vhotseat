"""Tickets (schedule.py, GPU scheduling slice 2, chunk A): taking a ticket, the order tickets are
served in (class, round-robin per requester, affinity with aging, admission and look-ahead), whose
turn it is, starting and finishing, liveness, and the max-hold yield. Fake clock; a temp SQLite
file; `alive` decides which pids are running."""

import datetime

import pytest

from vhotseat import schedule

# Monday 2026-11-16, local time.
MON = datetime.datetime(2026, 11, 16, 0, 0)


def at(day: int, hh: int, mm: int = 0, ss: int = 0) -> float:
    return (MON + datetime.timedelta(days=day, hours=hh, minutes=mm, seconds=ss)).timestamp()


@pytest.fixture
def sched(tmp_path):
    clock = {"t": at(0, 12)}
    live = {1, 2, 3, 4, 5, 6, 7, 8, 9}
    s = schedule.Schedule(tmp_path / "schedule.db", schedule.cfg({"default_class": {"comfy": "peter"}}),
                          now=lambda: clock["t"], alive=lambda pid: pid in live)
    s.clock, s.live = clock, live
    return s


def tick(s, seconds: float) -> None:
    s.clock["t"] += seconds


def ids(rows) -> list[int]:
    return [r["id"] for r in rows]


# --- taking a ticket ------------------------------------------------------------------------------

def test_take_stores_a_waiting_ticket(sched):
    t = sched.take("lead-38", "batch", "comfy", est_s=600, pid=1, model="sdxl", note="covers")
    assert t["state"] == "waiting" and t["requester"] == "lead-38" and t["class"] == "batch"
    assert t["workload"] == "comfy" and t["est_s"] == 600 and t["pid"] == 1 and t["model"] == "sdxl"
    assert t["created"] == t["heartbeat"] == sched.clock["t"] and t["note"] == "covers"
    assert sched.ticket(t["id"]) == t
    assert sched.ticket(999) is None


def test_take_refuses_an_unknown_class_or_no_workload(sched):
    with pytest.raises(schedule.ScheduleError, match="class"):
        sched.take("a", "vip", "comfy", pid=1)
    with pytest.raises(schedule.ScheduleError, match="workload"):
        sched.take("a", "batch", "", pid=1)


# --- the serving order ----------------------------------------------------------------------------

def test_higher_class_first_then_oldest(sched):
    b1 = sched.take("a", "batch", "comfy", pid=1)
    tick(sched, 1)
    g1 = sched.take("b", "background", "comfy", pid=2)
    tick(sched, 1)
    a1 = sched.take("c", "agent", "strata", pid=3)
    tick(sched, 1)
    b2 = sched.take("d", "batch", "comfy", pid=4)
    assert ids(sched.order()) == [a1["id"], b1["id"], b2["id"], g1["id"]]


def test_round_robin_per_requester_within_a_class(sched):
    # One requester with three tickets can't keep another with one waiting behind all three.
    x1 = sched.take("x", "batch", "comfy", pid=1)
    tick(sched, 1)
    x2 = sched.take("x", "batch", "comfy", pid=2)
    tick(sched, 1)
    x3 = sched.take("x", "batch", "comfy", pid=3)
    tick(sched, 1)
    y1 = sched.take("y", "batch", "comfy", pid=4)
    assert ids(sched.order()) == [x1["id"], y1["id"], x2["id"], x3["id"]]


def test_affinity_prefers_what_is_on_the_card(sched):
    s1 = sched.take("a", "batch", "strata", pid=1)
    tick(sched, 1)
    c1 = sched.take("b", "batch", "comfy", pid=2)
    # comfy is on the card: its ticket goes first, sparing a handover.
    assert ids(sched.order(on_card="comfy")) == [c1["id"], s1["id"]]
    assert ids(sched.order()) == [s1["id"], c1["id"]]          # nothing on the card: plain order


def test_affinity_ages_out(sched):
    s1 = sched.take("a", "batch", "strata", pid=1)
    tick(sched, 1)
    c1 = sched.take("b", "batch", "comfy", pid=2)
    tick(sched, 600)                                             # affinity_s defaults to 600
    # The strata ticket has waited 601 s, past affinity_s: it competes on age again and is older.
    assert ids(sched.order(on_card="comfy")) == [s1["id"], c1["id"]]


def test_affinity_never_beats_a_higher_class(sched):
    a1 = sched.take("a", "agent", "strata", pid=1)
    tick(sched, 1)
    c1 = sched.take("b", "batch", "comfy", pid=2)
    assert ids(sched.order(on_card="comfy")) == [a1["id"], c1["id"]]


def test_order_lists_only_waiting_tickets(sched):
    t1 = sched.take("a", "batch", "comfy", pid=1)
    t2 = sched.take("b", "batch", "comfy", pid=2)
    sched.start(t1["id"])
    assert ids(sched.order()) == [t2["id"]]


# --- whose turn -----------------------------------------------------------------------------------

def test_head_skips_a_ticket_a_booking_keeps_off(sched):
    sched.add_booking("Peter's day", at(0, 8), at(0, 18), cls="peter")
    a1 = sched.take("a", "agent", "strata", pid=1)
    tick(sched, 1)
    c1 = sched.take("b", "peter", "comfy", pid=2)
    head = sched.head()
    assert head["id"] == c1["id"]                                 # class peter is admitted
    assert sched.my_turn(c1["id"]) and not sched.my_turn(a1["id"])
    sched.clock["t"] = at(0, 18)
    assert sched.head()["id"] == c1["id"]                         # higher class still first
    sched.finish(c1["id"])
    assert sched.head()["id"] == a1["id"]


def test_head_looks_ahead_past_a_booking(sched):
    # A 2 h batch run at 17:00 would run into an 18:00-20:00 booking for strata only.
    sched.clock["t"] = at(0, 17)
    sched.add_booking("schoolmap", at(0, 18), at(0, 20), workloads=["strata"])
    long = sched.take("a", "batch", "comfy", est_s=7200, pid=1)
    tick(sched, 1)
    short = sched.take("b", "batch", "comfy", est_s=600, pid=2)
    assert sched.head()["id"] == short["id"]                      # the long one waits for 20:00
    assert not sched.my_turn(long["id"])


def test_head_uses_the_class_max_hold_when_there_is_no_estimate(sched):
    # No estimate: batch's max_hold_s (1200 s) is the estimate, so 17:45 + 20 min runs into 18:00.
    sched.clock["t"] = at(0, 17, 45)
    sched.add_booking("schoolmap", at(0, 18), at(0, 20), workloads=["strata"])
    t = sched.take("a", "batch", "comfy", pid=1)
    assert sched.head() is None and not sched.my_turn(t["id"])
    sched.clock["t"] = at(0, 20)
    assert sched.head()["id"] == t["id"]


def test_not_my_turn_while_another_ticket_runs(sched):
    t1 = sched.take("a", "batch", "comfy", pid=1)
    tick(sched, 1)
    t2 = sched.take("b", "batch", "comfy", pid=2)
    assert sched.my_turn(t1["id"])
    sched.start(t1["id"])
    assert sched.head()["id"] == t2["id"] and not sched.my_turn(t2["id"])
    sched.finish(t1["id"])
    assert sched.my_turn(t2["id"])


def test_head_is_none_with_no_tickets(sched):
    assert sched.head() is None
    assert not sched.my_turn(12345)


# --- starting and finishing -----------------------------------------------------------------------

def test_start_and_finish_set_state_and_times(sched):
    t = sched.take("a", "batch", "comfy", pid=1)
    tick(sched, 5)
    r = sched.start(t["id"])
    assert r["state"] == "running" and r["started"] == sched.clock["t"]
    with pytest.raises(schedule.ScheduleError):
        sched.start(t["id"])                                      # only a waiting ticket starts
    tick(sched, 5)
    r = sched.finish(t["id"], "yielded")
    assert r["state"] == "yielded" and r["ended"] == sched.clock["t"]
    with pytest.raises(schedule.ScheduleError):
        sched.finish(t["id"], "running")                          # not an end state
    with pytest.raises(schedule.ScheduleError):
        sched.finish(999)


# --- liveness -------------------------------------------------------------------------------------

def test_a_dead_process_drops_out(sched):
    t1 = sched.take("a", "batch", "comfy", pid=1)
    tick(sched, 1)
    t2 = sched.take("b", "batch", "comfy", pid=2)
    sched.start(t1["id"])
    sched.live.discard(1)                                          # the runner crashed
    assert sched.my_turn(t2["id"])
    assert sched.ticket(t1["id"])["state"] == "expired"


def test_a_ticket_without_a_pid_lives_by_its_heartbeat(sched):
    t = sched.take("remote", "batch", "comfy")                     # no pid: a caller elsewhere
    tick(sched, 100)
    sched.beat(t["id"])
    tick(sched, 100)
    assert ids(sched.order()) == [t["id"]]                         # beat 100 s ago: alive
    tick(sched, 30)
    assert sched.order() == []                                     # past STALE_S (120 s): expired
    assert sched.ticket(t["id"])["state"] == "expired"


# --- the max-hold yield ---------------------------------------------------------------------------

def test_overdue_only_past_max_hold_and_with_someone_waiting(sched):
    t = sched.take("a", "batch", "comfy", pid=1)
    sched.start(t["id"])
    tick(sched, 1300)                                              # past batch's 1200 s
    assert sched.overdue(t["id"]) is None                          # nobody waits: carry on
    sched.take("b", "background", "comfy", pid=2)
    assert sched.overdue(t["id"]) is None                          # only a lower class waits
    sched.take("c", "batch", "strata", pid=3)
    o = sched.overdue(t["id"])
    assert o == {"held_s": 1300, "limit_s": 1200, "waiting": 1}
    assert sched.should_yield(t["id"])


def test_a_class_without_max_hold_never_yields(sched):
    t = sched.take("a", "peter", "comfy", pid=1)
    sched.start(t["id"])
    tick(sched, 99999)
    sched.take("b", "peter", "strata", pid=2)
    assert sched.overdue(t["id"]) is None and not sched.should_yield(t["id"])


def test_overdue_is_none_for_a_ticket_that_is_not_running(sched):
    t = sched.take("a", "batch", "comfy", pid=1)
    assert sched.overdue(t["id"]) is None
    assert sched.overdue(999) is None


# --- the queue view -------------------------------------------------------------------------------

def test_queue_view_is_running_then_serving_order(sched):
    s1 = sched.take("a", "batch", "strata", pid=1)
    tick(sched, 1)
    c1 = sched.take("b", "batch", "comfy", pid=2)
    tick(sched, 1)
    r1 = sched.take("c", "agent", "comfy", pid=3)
    sched.start(r1["id"])
    assert ids(sched.queue_view(on_card="comfy")) == [r1["id"], c1["id"], s1["id"]]
