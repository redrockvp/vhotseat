"""Cancellation reaches a held process after it is ready to clean up."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vhotseat import arbiter
from vhotseat.kit import comfy_render


CHILD = """
import os, signal, sys, time
from pathlib import Path
from vhotseat.lock import GpuLock

folder, lock_path, mode = sys.argv[1:]
folder = Path(folder)
def stop(signum, frame):
    (folder / 'signal').write_text(signal.Signals(signum).name)
    if mode == 'ignore':
        return
    time.sleep(0.15)
    owner = GpuLock(Path(lock_path)).try_take('cleanup probe')
    if not isinstance(owner, str):
        with owner:
            owner = 'lock was free'
    (folder / 'cleaned').write_text(owner)
    sys.exit(23)

signal.signal(signal.SIGBREAK if os.name == 'nt' else signal.SIGTERM, stop)
(folder / 'ready').touch()
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    time.sleep(0.02)
sys.exit(77)  # safety net for a hold that never stops us
"""


@pytest.fixture
def cancel_on_wait(monkeypatch):
    """Deliver to the caller alone, on its main thread, after the real child is ready.

    Broadcasting Ctrl+Break from pytest would also signal an unisolated child directly
    and could make a wrapper that never forwards signals look correct.
    """
    signals = [signal.SIGINT, signal.SIGTERM,
               signal.SIGBREAK if os.name == "nt" else signal.SIGHUP]
    original = {sig: signal.getsignal(sig) for sig in signals}

    def install(ready, sig):
        sent = []

        class Process(subprocess.Popen):
            def __init__(self, command, *args, **kwargs):
                self.group_flags = kwargs.get("creationflags", 0)
                if command[0] == "vhotseat-test":
                    command = [sys.executable, str(Path(__file__).with_name("run_cli.py")),
                               *command[1:]]
                super().__init__(command, *args, **kwargs)

            def wait(self, *args, **kwargs):
                if not sent:
                    deadline = time.monotonic() + 15
                    while not ready():
                        assert self.poll() is None, "child exited before cancellation"
                        assert time.monotonic() < deadline, "child never became ready"
                        time.sleep(0.01)
                    sent.append((self, time.monotonic()))
                    signal.raise_signal(sig)
                return super().wait(*args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", Process)
        return sent

    yield install
    for sig, handler in original.items():
        signal.signal(sig, handler)


@pytest.mark.parametrize("mode", ["cleanup", "ignore"])
def test_hold_cancel(rig, tmp_path, monkeypatch, cancel_on_wait, mode):
    grace = 0.6
    monkeypatch.setattr(arbiter, "STOP_GRACE_S", grace, raising=False)
    sent = cancel_on_wait((tmp_path / "ready").exists, signal.SIGTERM)
    code = rig.arb.hold("comfy", [sys.executable, "-c", CHILD, str(tmp_path),
                                  str(rig.arb.lock.path), mode], wait=5)
    proc, start = sent[0]
    elapsed = time.monotonic() - start
    assert proc.poll() is not None, "hold returned with the child alive"
    if mode == "ignore":
        assert grace - 0.05 <= elapsed <= grace + 2, f"child stopped after {elapsed:.3f}s"
        assert code not in (0, 77), "child exited naturally"
    marker = tmp_path / "signal"
    assert marker.exists(), "child never received the polite signal"
    assert marker.read_text() == ("SIGBREAK" if os.name == "nt" else "SIGTERM")
    if mode == "cleanup":
        assert (tmp_path / "cleaned").read_text() == "hold comfy"
        assert code == 23
    if os.name == "nt":
        assert proc.group_flags & subprocess.CREATE_NEW_PROCESS_GROUP


def test_comfy_hold_cancel_reaches_renderer(rig, tmp_path, monkeypatch, cancel_on_wait):
    monkeypatch.setenv("VHOTSEAT_BIN", "vhotseat-test")
    monkeypatch.setenv("STRATA_URL", rig.strata.url)
    monkeypatch.setenv("COMFY_URL", rig.comfy.url)
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"1": {"class_type": "SaveImage", "inputs": {}}}] * 2))
    def finish_after_cleanup(_pid):
        rig.comfy.queue_posted.wait(20)

    rig.comfy.guard = finish_after_cleanup
    sent = cancel_on_wait(lambda: len(rig.comfy.clients) == 2 and len(rig.comfy.pending) == 1,
                          signal.SIGINT)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        try:
            comfy_render.main([str(jobs), "--url", rig.comfy.url, "--hold", "comfy",
                               "--timeout", "15"])
        except KeyboardInterrupt:
            pass  # the outer caller can propagate cancellation after cleanup
        running, pending = rig.comfy.clients
        assert any(pending in post.get("delete", []) for post in rig.comfy.queue_posts), (
            "outer comfy-render ended before the inner renderer deleted its pending prompt")
        assert time.monotonic() - sent[0][1] < 10, "cleanup waited for the render's own timeout"
        assert running in rig.comfy.history
    finally:
        rig.comfy.queue_posted.set()
