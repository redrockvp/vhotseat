"""What differs by platform: the lock across processes, and on Windows the GPU
counters, process command lines and machine RAM."""

import ctypes
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from vhotseat import autostart, gpu, host
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


def test_wddm_fills_in_missing_names():
    """NVML names no process on Windows; a process snapshot does, and unlike a handle it
    also names one we can't open (an elevated workload), so its VRAM can be attributed."""
    reader = gpu.WddmVram(_Reader([(1, None, ""), (2, 300, "known")]),
                          per_pid=lambda: {1: 20000}, names=lambda: {1: "strata.exe"})
    assert reader.read()[2] == [(1, 20000, "strata.exe"), (2, 300, "known")]


def test_wddm_skips_the_counters_when_nothing_is_missing():
    def boom():
        raise AssertionError("counters read")
    reader = gpu.WddmVram(_Reader([(1, 100, "a")]), per_pid=boom)
    assert reader.read()[2] == [(1, 100, "a")]

def test_an_invalid_snapshot_handle_is_a_failed_snapshot(monkeypatch):
    """INVALID_HANDLE_VALUE isn't spelled -1 once ctypes has had it: restype=c_void_p hands
    back 18446744073709551615 on a 64-bit build, so the check against -1 never matched and a
    failed process snapshot went on being read as if it had contents."""
    reads = []

    class FakeK32:
        pass

    k32 = FakeK32()
    reads = []
    k32.CreateToolhelp32Snapshot = lambda flags, pid: ctypes.c_void_p(-1).value

    def first(snap, entry):
        reads.append(snap)
        return 1                                  # as if the snapshot had contents

    k32.Process32FirstW = first
    k32.Process32NextW = lambda snap, entry: 0
    k32.CloseHandle = lambda h: None
    monkeypatch.setattr(host, "_kernel32", lambda: k32)
    assert host._snapshot_windows() == {}
    assert reads == []                       # the invalid handle was never used


@pytest.mark.skipif(os.name == "nt", reason="reads /proc")
def test_a_process_whose_image_cant_be_read_is_still_listed(monkeypatch, tmp_path):
    """readlink(/proc/<pid>/exe) fails for a process owned by somebody else. The table dropped
    the process instead of keeping it with exe=None, so a claim by exe name missed it."""
    d = tmp_path / "1234"
    d.mkdir()
    (d / "comm").write_text("somegame.exe" + chr(10))
    (d / "stat").write_text("1234 (somegame.exe) S 1 1 1 0 -1" + chr(10))
    real = os.scandir
    monkeypatch.setattr(os, "scandir", lambda path: real(str(tmp_path)) if path == "/proc" else real(path))

    def no_readlink(path):
        raise PermissionError(path)

    monkeypatch.setattr(os, "readlink", no_readlink)
    assert host._snapshot_posix()[1234] == {"pid": 1234, "ppid": 1, "name": "somegame.exe", "exe": None}


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


# --- the logon task that keeps the dashboard up (both paths, whichever we're on) ---


class _Done:
    def __init__(self, rc=0, out="Status: Running"):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _spy(fail=()):
    """A stand-in subprocess.run: records the argv it was handed, fails on request."""
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        joined = " ".join(argv)
        return _Done(1 if any(f in joined for f in fail) else 0)
    return calls, run


def test_autostart_windows_logon_task(tmp_path, capsys):
    """Windows has no systemd: Task Scheduler runs it at logon, quoting the exe path."""
    calls, run = _spy()
    exe = r"C:\Users\peter\.local\bin\vhotseat.exe"
    assert autostart.install("web", exe=exe, at="schtasks", run=run) == 0
    assert calls[0] == ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "vhotseat",
                        '/TR', f'"{exe}" web']
    assert autostart.status(at="schtasks", run=run) == 0
    assert autostart.remove(at="schtasks", run=run, vbs=tmp_path / "l.vbs") == 0
    assert ["schtasks", "/Delete", "/F", "/TN", "vhotseat"] in calls
    assert ["reg", "delete", autostart.RUN_KEY, "/v", "vhotseat", "/f"] in calls
    assert "no longer starts at logon" in capsys.readouterr().out


def test_autostart_windows_falls_back_to_the_run_key(tmp_path, capsys):
    """A logon trigger needs elevation. A normal shell gets the per-user Run key instead,
    through a script-host launcher: a console app in the Run key would leave a terminal
    window on the desktop all session."""
    exe = r"C:\Users\peter\.local\bin\vhotseat.exe"
    vbs = tmp_path / "vhotseat-autostart.vbs"
    calls, run = _spy(fail=("/Create",))
    assert autostart.install("web", exe=exe, at="schtasks", run=run, vbs=vbs) == 0
    assert f'Run """{exe}"" web", 0, False' in vbs.read_text()
    reg = [c for c in calls if c[:2] == ["reg", "add"]]
    assert reg and reg[0][1:] == ["add", autostart.RUN_KEY, "/v", "vhotseat", "/t", "REG_SZ",
                                  "/d", f'wscript.exe //B "{vbs}"', "/f"]
    assert "Task Scheduler refused" in capsys.readouterr().out
    assert autostart.remove(at="schtasks", run=run, vbs=vbs) == 0
    assert not vbs.exists()


def test_autostart_windows_status_when_nothing_registered(capsys):
    calls, run = _spy(fail=("/Query", "reg query"))
    assert autostart.status(at="schtasks", run=run) == 1
    assert "not registered" in capsys.readouterr().out


def test_autostart_systemd_user_unit(tmp_path, capsys):
    """Elsewhere it is one systemd user unit, and remove takes it back out."""
    calls, run = _spy()
    exe = "/home/peter/.local/bin/vhotseat"
    assert autostart.install("record", exe=exe, at="systemd", run=run, units=tmp_path) == 0
    unit = tmp_path / "vhotseat.service"
    assert f"ExecStart={exe} record" in unit.read_text()
    assert ["systemctl", "--user", "enable", "--now", "vhotseat.service"] in calls
    assert autostart.status(at="systemd", run=run, units=tmp_path) == 0
    assert autostart.remove(at="systemd", run=run, units=tmp_path) == 0
    assert not unit.exists()


def test_autostart_status_says_when_nothing_is_registered(tmp_path, capsys):
    """`status` is a scriptable answer: exit 1 when nothing was installed."""
    calls, run = _spy()
    assert autostart.status(at="systemd", run=run, units=tmp_path) == 1
    assert "not registered" in capsys.readouterr().out
