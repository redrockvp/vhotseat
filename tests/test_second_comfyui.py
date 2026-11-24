"""A second ComfyUI on the same card: its own workload, its own guard, its own VRAM.

The owner's box runs the main ComfyUI (port 8188, conda env `comfy`) and a dedicated one for
one project (port 8189, env `comfy-farcast`). Each is a `driver: comfyui` workload of its own, so
vhotseat gates both, counts each one's VRAM apart, and takes the card from either for the other,
for Strata, or for a game - without one ComfyUI's render ever being cut off by the other's job.

The guard node is shared code (one folder, linked into both installs' custom_nodes), so which
workload it switches comes from its environment: VHOTSEAT_GUARD_WORKLOAD, `comfy` when unset.
"""

import json
import sys
import time
from pathlib import Path

import pytest
from fakes import COMFY_CONTEXT_VRAM, COMFY_MODEL_VRAM, FakeComfy, FakeGpu, FakeHost, FakeStrata

from vhotseat import claims, doctor, gpu, host, setup, web
from vhotseat import config as cfgmod

ROOT = Path(__file__).resolve().parents[1]

COMFY_EXE = r"C:\Users\owner\anaconda3\envs\comfy\python.exe"
FARCAST_EXE = r"C:\Users\owner\anaconda3\envs\comfy-farcast\python.exe"


def farcast_workload(url: str) -> dict:
    """The second ComfyUI as the owner's config has it (a service here, so the fake host can say
    it's up; on Windows it has none and is found by its process)."""
    return {
        "driver": "comfyui", "label": "ComfyUI (Farcast)", "url": url,
        "service": {"systemd-user": "comfy-farcast"},
        "idle_vram_mib": 1500,
        "gate": {"kind": "plugin", "custom_nodes": "~/ComfyUI-Farcast/custom_nodes", "timeout": 300},
    }


@pytest.fixture
def frig(tmp_path, monkeypatch):
    """Strata, the main ComfyUI and the Farcast one, from config.example.yaml plus the third."""
    strata, comfy, farcast, h = FakeStrata(), FakeComfy(), FakeComfy(), FakeHost()
    h.active["comfy-farcast"] = True
    h.mem["comfy-farcast"] = {"ram_mib": 800, "swap_mib": 0, "file_mib": 0}
    g = FakeGpu(strata, comfy)
    g.more["farcast"] = farcast
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    raw = cfgmod.parse((ROOT / "config.example.yaml").read_text())
    raw["workloads"]["strata"]["url"] = strata.url
    raw["workloads"]["comfy"]["url"] = comfy.url
    raw["workloads"]["farcast"] = farcast_workload(farcast.url)
    raw["claims"], raw["claims_builtin"] = [], False
    cfg = cfgmod.validate(raw)
    arb = h.install(setup.build(cfg, gpu=g))
    strata.on_load = lambda: arb.gate("strata")

    class Rig:
        pass

    r = Rig()
    r.strata, r.comfy, r.farcast, r.host, r.gpu, r.arb, r.cfg = strata, comfy, farcast, h, g, arb, cfg
    r.state = tmp_path / "state" / "vhotseat"
    r.events = lambda: [json.loads(x) for x in
                        (r.state / "events.jsonl").read_text(encoding="utf-8").splitlines()] \
        if (r.state / "events.jsonl").exists() else []
    yield r
    for srv in (strata, comfy, farcast):
        srv.close()


def with_schedule(r, sched: dict):
    """The same rig with a `schedule:` block, as `vhotseat book` needs."""
    arb = r.host.install(setup.build(dict(r.cfg, schedule=sched), gpu=r.gpu))
    r.strata.on_load = lambda: arb.gate("strata")
    return arb


def running_render(fake: FakeComfy, seconds=1.0):
    fake.slow_s = seconds
    fake.queue_job({"1": {"class_type": "Slow", "inputs": {}}})
    while not fake.running:
        time.sleep(0.01)


# --- the config ------------------------------------------------------------------------------


def test_two_comfyui_workloads_validate(frig):
    assert {"strata", "comfy", "farcast"} <= set(frig.arb.workloads)
    assert type(frig.arb.workloads["farcast"]).__name__ == "ComfyUI"
    assert frig.arb.workloads["farcast"].url == frig.farcast.url


def test_each_comfyui_python_counts_as_its_own_workload():
    """The env names share a prefix: `envs\\comfy\\python.exe` must not swallow
    `envs\\comfy-farcast\\python.exe`, whichever order the workloads are listed in."""
    cmds = {
        11: ("", f"{COMFY_EXE} python main.py --disable-api-nodes --listen 100.86.162.40"),
        22: ("", f"{FARCAST_EXE} main.py --port 8189 --listen 127.0.0.1 --disable-partner-nodes"),
    }
    procs = [(11, 7000, "python.exe"), (22, 9000, "python.exe"), (33, 500, "dwm.exe")]
    sel = {"strata": ("process", "strata"),
           "comfy": ("process", r"anaconda3\envs\comfy\python.exe"),
           "farcast": ("process", r"anaconda3\envs\comfy-farcast\python.exe"),
           "unsloth": ("process", "unsloth")}
    for order in (list(sel), list(reversed(sel))):
        snap = gpu.attribute(24576, 8000, procs, {k: sel[k] for k in order},
                             procinfo=lambda pid: cmds.get(pid, ("", "")))
        assert (snap["comfy_mib"], snap["farcast_mib"], snap["other_mib"]) == (7000, 9000, 500), order


# --- handovers between the two ---------------------------------------------------------------


def test_switch_farcast_frees_the_main_comfyui_and_strata(frig):
    frig.comfy.vram = COMFY_MODEL_VRAM
    frig.strata.loaded = True
    assert frig.arb.switch("farcast", 10)["ok"]
    assert "POST /free" in frig.comfy.calls and frig.comfy.vram == COMFY_CONTEXT_VRAM
    assert not frig.strata.loaded


def test_switch_comfy_frees_farcast(frig):
    frig.farcast.vram = COMFY_MODEL_VRAM
    assert frig.arb.switch("comfy", 10)["ok"]
    assert "POST /free" in frig.farcast.calls and frig.farcast.vram == COMFY_CONTEXT_VRAM


def test_a_farcast_render_is_never_cut_off_by_a_main_comfyui_job(frig):
    running_render(frig.farcast, 2.0)
    res = frig.arb.switch("comfy", 0, request=True)
    assert not res["ok"]
    assert frig.farcast.running and "POST /free" not in frig.farcast.calls


def test_strata_gate_frees_an_idle_farcast(frig):
    frig.farcast.vram = COMFY_MODEL_VRAM
    assert frig.strata.request() == 200
    assert "POST /free" in frig.farcast.calls and frig.farcast.vram == COMFY_CONTEXT_VRAM


# --- games -----------------------------------------------------------------------------------


def game(arb, monkeypatch, on_start="release"):
    monkeypatch.setattr(arb, "claim_rules",
                        claims.rules([{"name": "EldenRing.exe", "on_start": on_start}], builtin=False))
    monkeypatch.setattr(arb, "claim_ignore", claims.ignores())
    procs = {4242: {"pid": 4242, "ppid": 4, "name": "EldenRing.exe", "exe": r"D:\Games\EldenRing.exe"}}
    monkeypatch.setattr(host, "processes", lambda: procs)


def test_a_game_starting_frees_the_card_from_farcast(frig, monkeypatch):
    game(frig.arb, monkeypatch)
    frig.farcast.vram = COMFY_MODEL_VRAM
    web.watch(frig.arb, polls=2)
    assert frig.farcast.vram == COMFY_CONTEXT_VRAM
    rel = [e for e in frig.events() if e["event"] == "claim-release"]
    assert rel and "farcast" in rel[0]["workloads"], rel


def test_a_farcast_job_is_refused_while_a_game_runs(frig, monkeypatch):
    game(frig.arb, monkeypatch)
    res = frig.arb.switch("farcast", 0, request=True)
    assert not res["ok"] and "EldenRing" in res["error"]


# --- bookings --------------------------------------------------------------------------------


def test_farcast_jobs_are_an_agents_when_the_config_says_so(frig):
    arb = with_schedule(frig, {"default_class": {"farcast": "agent"}})
    assert arb.schedule.class_of("farcast") == "agent"
    assert arb.schedule.class_of("comfy") == "peter"        # the web page's jobs stay the owner's


def test_a_farcast_booking_admits_farcast_and_keeps_strata_off(frig):
    arb = with_schedule(frig, {"default_class": {"farcast": "agent"}})
    now = time.time()
    arb.schedule.add_booking("farcast-lead", now - 60, now + 3600, workloads=["farcast"])
    assert arb.switch("farcast", 0, load=False, request=True)["ok"]
    assert not arb.gate("strata")


def test_the_owners_booking_keeps_farcast_off_but_not_the_web_page(frig):
    arb = with_schedule(frig, {"default_class": {"farcast": "agent"}})
    now = time.time()
    arb.schedule.add_booking("owner", now - 60, now + 3600, cls="peter")
    res = arb.switch("farcast", 0, load=False, request=True)
    assert not res["ok"] and "booked" in res["error"]
    assert arb.switch("comfy", 0, load=False, request=True)["ok"]


def test_book_works_once_the_schedule_block_is_there(frig, capsys):
    from vhotseat import cli
    arb = with_schedule(frig, {"default_class": {"farcast": "agent"}})
    tomorrow = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
    assert cli.main(["book", "farcast-lead", f"{tomorrow} 10:00", f"{tomorrow} 11:30",
                     "--workload", "farcast", "--note", "Farcast install"],
                    arbiter=arb, cfg=dict(frig.cfg, schedule={})) == 0
    b = arb.schedule.bookings()[-1]
    assert b["workloads"] == ["farcast"] and b["class"] == "booked"


# --- doctor ----------------------------------------------------------------------------------


def test_doctor_checks_the_farcast_guard_in_its_own_custom_nodes(frig, tmp_path):
    nodes = tmp_path / "farcast_nodes"
    (nodes / "vhotseat_guard").mkdir(parents=True)
    (nodes / "vhotseat_guard" / "__init__.py").write_text("")
    frig.arb.workloads["farcast"].gate["custom_nodes"] = str(nodes)
    results = {c: s for s, c, _ in doctor.checks(frig.arb, frig.cfg)}
    assert results["farcast: gate"] == "ok"
    (nodes / "vhotseat_guard" / "__init__.py").unlink()
    results = {c: s for s, c, _ in doctor.checks(frig.arb, frig.cfg)}
    assert results["farcast: gate"] != "ok"


# --- the guard: which workload it switches ---------------------------------------------------


def load_guard(monkeypatch, workload=None):
    sys.path.insert(0, str(ROOT / "integrations" / "comfyui"))
    if workload is None:
        monkeypatch.delenv("VHOTSEAT_GUARD_WORKLOAD", raising=False)
    else:
        monkeypatch.setenv("VHOTSEAT_GUARD_WORKLOAD", workload)
    monkeypatch.setenv("VHOTSEAT_BIN", "vhotseat-under-test")
    sys.modules.pop("vhotseat_guard", None)
    import vhotseat_guard  # its install() logs "ComfyUI internals changed" here: no ComfyUI
    return vhotseat_guard


class Res:
    def __init__(self, code, err=""):
        self.returncode, self.stderr, self.stdout = code, err, ""


def recorded(guard, monkeypatch, code=0, err=""):
    calls = []
    monkeypatch.setattr(guard, "REQUEST_OK", True)
    monkeypatch.setattr(guard.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), Res(code, err))[1])
    return calls


def test_the_guard_switches_comfy_by_default(monkeypatch):
    guard = load_guard(monkeypatch)
    calls = recorded(guard, monkeypatch)
    guard.take_gpu()
    assert calls[0][:3] == ["vhotseat-under-test", "switch", "comfy"] and "--request" in calls[0]


def test_the_guard_switches_the_workload_its_environment_names(monkeypatch):
    guard = load_guard(monkeypatch, "farcast")
    calls = recorded(guard, monkeypatch)
    guard.take_gpu()
    assert calls[0][:3] == ["vhotseat-under-test", "switch", "farcast"] and "--request" in calls[0]


def test_the_guard_names_its_workload_when_vhotseat_says_nothing(monkeypatch):
    guard = load_guard(monkeypatch, "farcast")
    recorded(guard, monkeypatch, code=1)
    with pytest.raises(guard.GpuBusy, match="vhotseat switch farcast exited 1"):
        guard.take_gpu()


@pytest.fixture(autouse=True)
def _forget_guard():
    yield
    sys.modules.pop("vhotseat_guard", None)
    p = str(ROOT / "integrations" / "comfyui")
    while p in sys.path:
        sys.path.remove(p)
