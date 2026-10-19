"""What differs by platform: the lock across processes, and on Windows the GPU
counters, process command lines and machine RAM."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vhotseat import gpu, host
from vhotseat.lock import GpuLock

ROOT = Path(__file__).resolve().parent.parent

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows only")

HOLD = """
import sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from vhotseat.lock import GpuLock
with GpuLock(Path(sys.argv[1])).try_take("switch comfy"):
    print("held", flush=True)
    sys.stdin.read()
"""


def test_lock_across_processes(tmp_path):
    """Another process holding the lock: try_take returns its note, holder() reads
    it without touching the lock, and the lock is free once that process lets go."""
    path = tmp_path / "switch.lock"
    p = subprocess.Popen([sys.executable, "-c", HOLD, str(path), str(ROOT / "src")],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        lock = GpuLock(path)
        assert lock.try_take("switch strata") == "switch comfy"
        assert lock.holder() == "switch comfy"
    finally:
        p.stdin.close()
        p.wait(10)
    got = lock.try_take("switch strata")
    assert not isinstance(got, str)
    with got:
        assert lock.holder() == "switch strata"
    assert lock.holder() == ""


def test_generation_bumps(tmp_path):
    lock = GpuLock(tmp_path / "switch.lock")
    assert lock.generation() == 0
    assert lock.bump() == 1 and lock.bump() == 2
    assert lock.generation() == 2


class _Reader:
    name = "fake"

    def __init__(self, procs):
        self.procs = procs

    def read(self):
        return 24576, 1000, self.procs

    def health(self):
        return {"name": "fake"}


def test_wddm_fills_in_missing_vram():
    """Processes the driver reports no VRAM for get the counters' figure; one it
    does report keeps its own; one the counters don't know stays unknown."""
    reader = gpu.WddmVram(_Reader([(1, None, "a"), (2, 300, "b"), (3, None, "c")]),
                          per_pid=lambda: {1: 20000, 2: 999})
    assert reader.read() == (24576, 1000, [(1, 20000, "a"), (2, 300, "b"), (3, None, "c")])
    assert reader.name == "fake" and reader.health() == {"name": "fake"}


def test_wddm_skips_the_counters_when_nothing_is_missing():
    def boom():
        raise AssertionError("counters read")
    reader = gpu.WddmVram(_Reader([(1, 100, "a")]), per_pid=boom)
    assert reader.read()[2] == [(1, 100, "a")]


@windows_only
def test_windows_machine_ram():
    ram = host.machine_ram()
    assert "error" not in ram
    assert 0 < ram["available_mib"] <= ram["total_mib"]
    assert ram["swap_total_mib"] >= 0 and ram["swap_used_mib"] >= 0


@windows_only
def test_windows_command_line_of_a_process():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--marker-arg"])
    try:
        time.sleep(0.2)
        cgroup, cmd = gpu.read_proc(p.pid)
        assert cgroup == ""
        assert cmd.lower().startswith(str(Path(sys.executable).resolve()).lower()) or "python" in cmd.lower()
        assert "--marker-arg" in cmd
    finally:
        p.kill()
        p.wait()
    assert gpu.read_proc(p.pid) == ("", "")


@windows_only
def test_windows_gpu_counters():
    """The GPU Process Memory counters read, as a pid -> MiB map (empty with no GPU)."""
    try:
        per = gpu.wddm_process_vram()
    except gpu.GpuError as e:
        pytest.skip(f"no GPU counters here: {e}")
    assert all(isinstance(pid, int) and mib >= 0 for pid, mib in per.items())
