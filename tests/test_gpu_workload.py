"""vhotseat's behaviour on the Strata + ComfyUI setup (config.example.yaml),
end to end against fake Strata and ComfyUI servers.

Each race and deadlock in docs/how-it-works.md §3-4 is a scenario here, as are
the ComfyUI restart cases from the README (tested by hand on 2026-10-16).
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def last(r, kind=None):
    evs = [e for e in r.events() if kind is None or e["event"] == kind]
    return evs[-1] if evs else None


# --- status ------------------------------------------------------------------


def test_status_modes(rig, capsys):
    assert rig.arb.probe()["mode"] == "none"
    rig.strata.loaded = True
    assert rig.arb.probe()["mode"] == "strata"
    rig.comfy.vram = 7000
    assert rig.arb.probe()["mode"] == "conflict"


def test_status_json_shape_is_unchanged(rig, capsys):
    """A dashboard reads these keys (monitor.ts)."""
    assert rig.cli("status", "--json") == 0
    s = json.loads(capsys.readouterr().out)
    assert set(s) == {"mode", "strata", "comfy", "gpu", "ram", "last_event"}
    assert set(s["strata"]) == {"service", "reachable", "loaded"}
    assert set(s["comfy"]) == {"service", "reachable", "running", "pending", "holds_models"}
    assert set(s["gpu"]) == {"total_mib", "free_mib", "strata_mib", "comfy_mib", "other_mib"}
    assert set(s["ram"]) >= {"total_mib", "available_mib", "strata", "comfy"}


def test_status_text(rig, capsys):
    rig.strata.loaded = True
    rig.cli("status")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "mode:   strata"
    assert out[1] == "strata: model loaded"
    assert out[2] == "comfy:  no models loaded, 0 running / 0 queued"
    assert out[3].startswith("gpu:    1903 of 16303 MiB free (strata 14000, comfy 400, other 0)")


# --- switch --------------------------------------------------------------------


def test_switch_strata_frees_comfy_then_loads(rig):
    rig.comfy.vram = 7000
    assert rig.cli("switch", "strata") == 0
    assert rig.strata.loaded and rig.comfy.vram == 400
    assert "POST /free" in rig.comfy.calls
    assert last(rig, "switch")["to"] == "strata"
    # Strata's own hook ran during /load and allowed it: the switch asked for this load.
    assert last(rig, "strata-load")["allowed"] is True
    assert last(rig, "strata-load")["reason"] == "switch strata in progress"


def test_switch_comfy_unloads_strata(rig):
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy") == 0
    assert not rig.strata.loaded
    assert last(rig, "switch")["previous"] == "strata"


def test_switch_is_idempotent(rig):
    rig.strata.loaded = True
    assert rig.cli("switch", "strata") == 0
    assert "POST /load" not in rig.strata.calls
    assert last(rig, "switch") is None                   # nothing changed, nothing logged


def test_switch_comfy_always_asks_strata(rig):
    """§3.3: Strata may be mid-load with nothing on the GPU yet; /unload waits behind it."""
    assert rig.cli("switch", "comfy") == 0
    assert "POST /unload" in rig.strata.calls


def test_switch_comfy_refused_while_strata_answers(rig, capsys):
    rig.strata.loaded = True
    t = rig.strata.request_async(0.5)
    time.sleep(0.05)
    assert rig.cli("switch", "comfy", "--json") == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "answering or loading" in out["error"]
    t.join()


def test_switch_comfy_waits_for_strata_answer(rig):
    """An answer is never interrupted: --wait lets it finish, then unloads."""
    rig.strata.loaded = True
    t = rig.strata.request_async(0.3)
    time.sleep(0.05)
    assert rig.cli("switch", "comfy", "--wait", "5") == 0
    t.join()
    assert t.result == 200 and not rig.strata.loaded


def test_switch_strata_refused_while_comfy_has_jobs(rig):
    rig.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})
    assert rig.cli("switch", "strata") == 1
    assert "jobs running or queued" in last(rig, "switch")["error"]
    assert not rig.strata.loaded


def test_switch_starts_stopped_services(rig):
    rig.host.active["strata"] = False
    assert rig.cli("switch", "strata") == 0
    assert rig.host.starts == ["strata"]


def test_stopped_strata_holding_vram_is_reported(rig):
    rig.host.active["strata"] = False
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy") == 1
    assert "strata.service is stopped; kill it by hand" in last(rig, "switch")["error"]


# --- Strata's hook (a request wakes the lazy model) ------------------------------


def test_hook_allows_when_comfy_idle(rig):
    assert rig.strata.request() == 200
    e = last(rig, "strata-load")
    assert e["allowed"] and e["source"] == "request" and e["reason"] == "ComfyUI idle"
    assert "available_mib" in e and "comfy" in e


def test_hook_frees_idle_comfy_models(rig):
    rig.comfy.vram = 7000
    assert rig.strata.request() == 200
    assert rig.comfy.vram == 400
    assert last(rig, "strata-load")["reason"] == "ComfyUI idle; freed its 7000 MiB of models"


def test_hook_refuses_while_comfy_renders(rig):
    """A render is never interrupted: Strata answers 503 instead."""
    rig.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})
    time.sleep(0.05)
    assert rig.strata.request() == 503
    e = last(rig, "strata-load")
    assert not e["allowed"] and e["reason"] == "ComfyUI has 1 job(s) running or queued"


def test_hook_refuses_when_comfy_state_unknown(rig):
    """Fail closed: ComfyUI holding models with its API down could be mid-job.
    (gpu-workload used to read an unreachable /queue as 0 jobs.)"""
    rig.comfy.vram = 7000
    rig.comfy.down = True
    assert rig.strata.request() == 503
    assert "whether it has jobs is unknown" in last(rig, "strata-load")["reason"]


def test_hook_allows_when_comfy_down_but_holding_nothing(rig):
    """…but a ComfyUI that's starting up (API not answering yet, nothing on the
    GPU) doesn't keep Strata out."""
    rig.comfy.down = True
    assert rig.strata.request() == 200


def test_hook_refuses_when_nvidia_smi_fails(rig):
    rig.gpu.error = "nvidia-smi failed: boom"
    assert rig.strata.request() == 503
    assert last(rig, "strata-load")["reason"] == "nvidia-smi failed: boom"


def test_hook_refuses_during_switch_comfy(rig):
    with rig.arb.lock.try_take("switch comfy"):
        assert rig.strata.request() == 503
    e = last(rig, "strata-load")
    assert e["source"] == "hook" and e["reason"] == "switch comfy in progress"


# --- races (how-it-works §3) --------------------------------------------------------


def test_race_job_queued_as_hook_frees(rig):
    """§3.1: a job lands between the hook's /queue check and its /free. /free is
    postponed past the job, ComfyUI's VRAM doesn't drop, and the hook refuses
    rather than letting the two share the GPU. ComfyUI is not restarted."""
    rig.comfy.vram = 7000
    rig.comfy.slow_s = 6.0
    rig.comfy.job_arrives_on_free = True
    assert rig.strata.request() == 503
    e = last(rig, "strata-load")
    assert not e["allowed"] and "could not make room in ComfyUI" in e["reason"]
    assert rig.host.restarts == []


def test_race_job_after_hook_allowed(rig):
    """§3.1, second half: a job queued after Strata loaded waits (--wait) for
    Strata's answer, then unloads it, then runs."""
    t = rig.strata.request_async(0.3)
    time.sleep(0.1)
    assert rig.strata.loaded
    assert rig.cli("switch", "comfy", "--wait", "5") == 0
    t.join()
    assert t.result == 200 and not rig.strata.loaded


def test_race_interleaved_jobs_and_request(rig):
    """§3.4: job A done, Strata request, job B: two extra handovers, both safe."""
    rig.comfy.vram = 7000                              # job A finished, models still loaded
    assert rig.strata.request() == 200                 # hook frees ComfyUI, Strata loads
    assert rig.cli("switch", "comfy", "--wait", "5") == 0  # job B's guard
    assert not rig.strata.loaded


# --- deadlocks (how-it-works §4) ------------------------------------------------------


def test_switch_strata_does_not_deadlock_on_its_own_hook(rig):
    """§4.1: switch strata holds the lock and blocks on /load, whose hook wants
    the lock. The hook reads the holder note instead of waiting."""
    done = threading.Event()
    threading.Thread(target=lambda: (rig.cli("switch", "strata"), done.set()), daemon=True).start()
    assert done.wait(5)
    assert rig.strata.loaded


def test_switch_inside_hold_is_a_noop(rig, monkeypatch, capsys):
    """§4.2: a hold's command switching to the held mode."""
    monkeypatch.setenv("VHOTSEAT_HOLD", "comfy")
    assert rig.cli("switch", "comfy", "--json") == 0
    assert json.loads(capsys.readouterr().out)["held"] == "comfy"
    assert rig.cli("switch", "strata") == 1


def test_hold_refuses_strata_until_command_exits(rig, tmp_path):
    rig.strata.loaded = True
    gate = tmp_path / "go"
    seen = {}

    def during():
        while not gate.exists():
            time.sleep(0.01)
        seen["status"] = rig.strata.request()

    t = threading.Thread(target=during, daemon=True)
    t.start()
    code = rig.cli("hold", "comfy", "--", sys.executable, "-c",
                   f"import pathlib,time; pathlib.Path({str(gate)!r}).touch(); time.sleep(0.5)")
    t.join()
    assert code == 0 and seen["status"] == 503
    assert not rig.strata.loaded
    assert [e["event"] for e in rig.events() if e["event"].startswith("hold")] == ["hold", "hold-end"]


def test_hold_exit_code_is_the_commands(rig):
    assert rig.cli("hold", "comfy", "--", sys.executable, "-c", "raise SystemExit(7)") == 7


def test_guard_inside_hold_does_not_wait_for_it(rig, tmp_path):
    """The bug in Appendix A.6 #1: a script under `hold comfy` queues a ComfyUI
    job; the job's guard runs `switch comfy` from ComfyUI's process (no
    VHOTSEAT_HOLD in its env). It used to wait for the hold's lock, while
    the script waited for the job: stuck until the guard's 420 s timeout."""
    ready = tmp_path / "ready"
    result = {}

    def guard():
        while not ready.exists():
            time.sleep(0.01)
        t0 = time.monotonic()
        result["code"] = rig.arb.switch("comfy", 300)
        result["s"] = time.monotonic() - t0

    t = threading.Thread(target=guard, daemon=True)
    t.start()
    rig.cli("hold", "comfy", "--", sys.executable, "-c",
            f"import pathlib,time; pathlib.Path({str(ready)!r}).touch(); time.sleep(1)")
    t.join(5)
    assert result["code"] == {"ok": True, "held": "comfy"}
    assert result["s"] < 0.9                           # didn't wait for the hold to end


def test_switch_waits_for_another_switch(rig):
    """Concurrent switches are serialized."""
    with rig.arb.lock.try_take("switch strata"):
        done = threading.Event()
        threading.Thread(target=lambda: (rig.cli("switch", "comfy"), done.set()), daemon=True).start()
        assert not done.wait(0.3)
    assert done.wait(5)


def test_hook_and_guard_wait_is_bounded(rig):
    """§4.3: the hook holds the lock while it frees ComfyUI; a guard starting
    then waits for it, and is never stuck."""
    rig.comfy.vram = 7000
    t = rig.strata.request_async(0.2)
    while "POST /free" not in rig.comfy.calls:        # the hook has the lock
        time.sleep(0.005)
    res = rig.arb.switch("comfy", 10)
    t.join()
    assert res["ok"] and t.result == 200 and not rig.strata.loaded


# --- ComfyUI restart (README "ComfyUI restart: RAM, not just VRAM") ----------------------


def test_restart_when_strata_needs_comfys_ram(rig):
    """Strata's admission isn't met, last job finished 4 s ago: waits ~6 s more
    (so its result can be read from /history), restarts ComfyUI."""
    pid = rig.comfy.queue_job()
    time.sleep(0.3)
    rig.comfy.finish_history(pid, ago=4)
    rig.comfy.vram = 7000
    rig.host.mem["comfyui"] = {"ram_mib": 6000, "swap_mib": 1000, "file_mib": 0}
    rig.host.available_mib = 42000
    rig.host.on_restart["comfyui"] = lambda: (rig.comfy.restarted(), setattr(rig.host, "available_mib", 49000))
    assert rig.strata.request() == 200
    assert rig.host.restarts == ["comfyui"]
    e = last(rig, "strata-load")
    assert e["reason"] == ("ComfyUI idle; freed its 7000 MiB of models; "
                           "restarted ComfyUI to release 7000 MiB of RAM + swap for Strata")


def test_no_restart_when_ram_is_enough(rig):
    rig.comfy.vram = 7000
    rig.host.mem["comfyui"] = {"ram_mib": 9000, "swap_mib": 500, "file_mib": 0}
    assert rig.strata.request() == 200
    assert rig.host.restarts == []


def test_no_restart_if_job_queued_during_settle(rig):
    pid = rig.comfy.queue_job()
    time.sleep(0.3)
    rig.comfy.finish_history(pid, ago=0)
    rig.host.available_mib = 42000
    threading.Timer(0.3, lambda: rig.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})).start()
    assert rig.strata.request() == 503
    assert rig.host.restarts == []
    assert "queued just now; not restarting it" in last(rig, "strata-load")["reason"]


def test_restart_when_vram_stuck_after_free(rig):
    """An extension that doesn't unload fully (IndexTTS without our patch)."""
    rig.comfy.vram = 7000
    rig.comfy.stuck_vram = 2066
    assert rig.strata.request() == 200
    assert rig.host.restarts == ["comfyui"]
    assert last(rig, "strata-load")["reason"] == "ComfyUI idle; restarted it: kept 2066 MiB of VRAM after /free"


def test_switch_strata_restarts_too(rig):
    rig.comfy.vram = 7000
    rig.host.available_mib = 42000
    rig.host.on_restart["comfyui"] = lambda: (rig.comfy.restarted(), setattr(rig.host, "available_mib", 49000))
    assert rig.cli("switch", "strata") == 0
    assert rig.host.restarts == ["comfyui"] and rig.strata.loaded


# --- the guard node -----------------------------------------------------------------------


def _guard(rig, monkeypatch):
    """The real guard node's take_gpu(), running vhotseat as a subprocess
    (tests/run_cli.py: the real CLI with GPU and services read from the fakes)."""
    sys.path.insert(0, str(ROOT / "comfyui"))
    monkeypatch.setenv("GPU_WORKLOAD_BIN", str(ROOT / "tests" / "run_cli.py"))
    monkeypatch.setenv("STRATA_URL", rig.strata.url)
    monkeypatch.setenv("COMFY_URL", rig.comfy.url)
    monkeypatch.setenv("XDG_STATE_HOME", str(rig.state.parent))
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    monkeypatch.setenv("GPU_WORKLOAD_GUARD_WAIT_S", "2")
    sys.modules.pop("gpu_workload_guard", None)
    import gpu_workload_guard  # its install() logs "ComfyUI internals changed" here: no ComfyUI
    return gpu_workload_guard


def test_guard_unloads_strata_before_a_job(rig, monkeypatch):
    guard = _guard(rig, monkeypatch)
    rig.strata.loaded = True
    guard.take_gpu()
    assert not rig.strata.loaded


def test_guard_reports_why_gpu_is_busy(rig, monkeypatch):
    guard = _guard(rig, monkeypatch)
    # The guard waits up to 2 s (GPU_WORKLOAD_GUARD_WAIT_S) for Strata's answer;
    # this one takes longer. (The fake's waits aren't scaled down like the CLI's.)
    rig.strata.loaded = True
    t = rig.strata.request_async(4.0)
    time.sleep(0.05)
    with pytest.raises(guard.GpuBusy, match="Strata is answering or loading"):
        guard.take_gpu()
    t.join()
