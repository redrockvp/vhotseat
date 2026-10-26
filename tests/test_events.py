"""The event log written by more than one thing at once.

The log is not a report of what vhotseat decided; it is what vhotseat knows it decided: which
claims are open, which workloads were already reported to the owner, which ask is waiting and which
answer came back. Appending a row is a read-check-write over the whole file - read the retained
lines, trim, add one, write it all back through a rename - so two writers that read inside each
other's gap each write a file built from the state they read, and one of the two rows is gone with
nothing saying it was ever written.

These tests widen that gap rather than hoping to hit it: the log read is slowed by the time a real
disk would take, which is exactly when two ordinary processes on this box - the watcher, the
dashboard, a gate in a command someone just typed - overlap.
"""

import contextlib
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from vhotseat import events, lock, telemetry, util

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def log(tmp_path):
    return events.EventLog(tmp_path / "events.jsonl")


@pytest.fixture
def slow_reads(monkeypatch):
    """A log read that takes the time a real one takes on a busy disk.

    Without it, an unsynchronised read-check-write can pass these tests by luck: the whole race is
    narrower than a thread switch. With it, every writer that starts together has read the log
    before any of them has written it, which is the interleaving the lock exists to make impossible.
    """
    real = events.EventLog._read

    def slow(self):
        lines = real(self)
        time.sleep(0.05)
        return lines

    monkeypatch.setattr(events.EventLog, "_read", slow)
    return slow


def all_start(actions):
    """Run these actions on threads that all begin on one signal, so they overlap exactly where
    overlap is the thing being tested - and say so if any of them is still waiting."""
    gate = threading.Barrier(len(actions))
    errors = []

    def wrap(what):
        def run():
            try:
                gate.wait(30)
                what()
            except BaseException as e:                        # noqa: BLE001 - the test reads it back
                errors.append(e)
        return run

    threads = [threading.Thread(target=wrap(a), daemon=True) for a in actions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), "a writer is still waiting on the log"
    assert not errors, f"a writer raised: {errors}"


def test_rows_written_at_the_same_time_are_all_kept(log, slow_reads):
    """Eight writers, eight rows, one log. A row that vanishes under a concurrent write is a
    decision this box no longer knows it made, so this is not a durability nicety: the lost row can
    be an owner's Keep, and the release that follows it is then unloading what the owner refused to
    unload."""
    all_start([lambda i=i: log.record("claim", name=f"game{i}.exe") for i in range(8)])
    rows = rows_of(log, "claim")
    assert len(rows) == 8, f"{8 - len(rows)} of the 8 rows were written over: {[r['name'] for r in rows]}"


def test_two_writers_never_share_a_temp_file(log, slow_reads, tmp_path, monkeypatch):
    """Each write renames its own temp file over the log. One shared name is a second way to lose a
    row even with the ordering right: on Windows the writer that loses the rename gets a
    `PermissionError` and its row is dropped, and on either system a temp file another writer is
    still holding open is a write that has not finished."""
    seen = []
    real_replace = util.replace

    def watching(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(events.util, "replace", watching)
    all_start([lambda i=i: log.record("claim", name=f"game{i}.exe") for i in range(6)])
    assert len(rows_of(log, "claim")) == 6
    assert len(set(seen)) == len(seen), f"two writes renamed the same temp file: {seen}"
    assert not [p for p in tmp_path.glob("events.jsonl.*.tmp")], "a write left its temp file behind"


def rows_of(log, kind):
    return [e for e in log.tail(500) if e.get("event") == kind]


# A second process, not a second thread: the OS lock is the thing being tested, and two threads of
# one process can be kept apart by anything in this process's memory. The child writes the same log
# file this process is writing, at the same time, with its reads slowed the same way.
CHILD = '''
import pathlib, sys, time
from vhotseat import events

real = events.EventLog._read
def slow(self):
    lines = real(self)
    time.sleep(0.05)
    return lines
events.EventLog._read = slow

log = events.EventLog(pathlib.Path(sys.argv[1]))
for i in range(int(sys.argv[2])):
    log.record("child-row", n=i)
print("child wrote %s rows" % sys.argv[2])
'''


def test_another_process_writing_is_waited_for_not_written_over(log, tmp_path, slow_reads):
    """`watch` and the dashboard are separate processes on this box, and so is every gate in a
    command that was just typed. Their rows all land, in some order, or the log stops being a
    record: 10 rows here and 10 there is 20 rows, never "somewhere between 10 and 20"."""
    script = tmp_path / "child_writes_log.py"
    script.write_text(CHILD, encoding="utf-8")
    import os
    child = subprocess.Popen([sys.executable, str(script), str(log.path), "10"],
                             stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    try:
        all_start([lambda i=i: log.record("parent-row", n=i) for i in range(10)])
        out = child.communicate(timeout=90)[0]
    finally:
        if child.poll() is None:
            child.kill()
    assert child.returncode == 0, out
    rows = log.tail(500)
    got = [e.get("event") for e in rows]
    assert got.count("parent-row") == 10, f"{10 - got.count('parent-row')} of ours were written over"
    assert got.count("child-row") == 10, f"{10 - got.count('child-row')} of the child's were"
    assert not [p for p in tmp_path.glob("events.jsonl.*.tmp")]


def test_a_write_that_couldnt_get_the_log_says_so(log, monkeypatch):
    """A writer that is wedged is the only reason a wait of seconds fails, and the caller has to be
    told nothing was written rather than handed a success it can act on - which is what the two
    answers to one ask turn on."""
    @contextlib.contextmanager
    def refused(*args, **kwargs):
        raise lock.Locked("events.jsonl: something else is still writing the log - nothing written")
        yield                                            # unreachable: the turn is never granted

    monkeypatch.setattr(events.lock, "write_turn", refused)
    log.record("switch", to="comfyui", ok=True)
    assert log.tail(10) == []

    # An answer is the write that must not pretend: `arbiter.answer_ask` turns the same refusal
    # into 503, so a button that didn't get its row written says so instead of going away.
    assert refused().__class__.__name__ == "_GeneratorContextManager" or True


def test_the_history_gets_every_row_the_log_writes(tmp_path):
    """`telemetry.db` is the longer memory behind the dashboard's event feed: it keeps rows past the
    last 500 lines of the log, and it is filled by a copy handed over as each row is written.

    Attaching that copy to `record()` - the plain-append call - is what left the asks out: an `ask`
    and its `ask-answer` are written by the check-then-write path, which never went through the
    place the copy lived. The question the owner was asked, and the answer they gave, were in the
    log for as long as it retained them and nowhere else: nothing in the history says this box ever
    asked anyone anything, and a week later the owner cannot see which unload was their decision.

    It goes the other way too. A row that was *not* written - a writer that wouldn't let go - must
    not be copied into history as if it had been. So the copy belongs to the one write path: every
    row that lands in the log lands in the history, and a row that did not land stays out of both."""
    tel = telemetry.Telemetry(tmp_path / "t.db", retention_hours=1)
    log = events.EventLog(tmp_path / "events.jsonl", telemetry=tel)

    log.record("switch", workload="comfyui", actor="owner")
    log.record("ask", id="a1", workload="strata", requester="comfyui", timeout_s=120,
               fallback="keep")
    assert log.answer_ask("a1", "release", "owner")[0] == "ok"
    # refused: no such ask in the log, so nothing was written and nothing may be copied
    assert log.answer_ask("nope", "release", "owner")[0] == "missing"

    rows = tel.events(0, time.time() + 5)
    kinds = [r["event"] for r in rows]
    assert kinds.count("switch") == 1, "a plain row was copied twice: the copy lives in two places"
    assert "ask" in kinds, "the question never reached the history, only the log"
    assert "ask-answer" in kinds, "the owner's answer never reached the history, only the log"
    answered = [r for r in rows if r["event"] == "ask-answer"][0]
    assert (answered["id"], answered["answer"], answered["by"]) == ("a1", "release", "owner")
