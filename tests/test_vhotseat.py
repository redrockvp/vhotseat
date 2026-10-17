"""Phase 1: the config file, the http driver, admission, `switch none`, the
`vhotseat` CLI, GPU attribution and doctor."""

import json
import textwrap
import time
from pathlib import Path

import pytest

from fakes import FakeComfy, FakeGpu, FakeHost, FakeStrata
from vhotseat import cli, config, doctor, gpu, setup
from vhotseat.drivers.http import lookup, matches, MISSING

ROOT = Path(__file__).resolve().parent.parent


def config_for(strata_url, comfy_url, **strata_extra):
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    raw["workloads"]["strata"]["url"] = strata_url
    raw["workloads"]["comfy"]["url"] = comfy_url
    raw["workloads"]["strata"].update(strata_extra)
    return config.validate(raw)


@pytest.fixture
def vh(tmp_path, monkeypatch):
    """The vhotseat CLI on config.example.yaml, against the fakes."""
    strata, comfy, host = FakeStrata(), FakeComfy(), FakeHost()
    host.on_restart["comfyui"] = comfy.restarted
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = config_for(strata.url, comfy.url)

    class R:
        pass

    r = R()
    r.strata, r.comfy, r.host, r.cfg = strata, comfy, host, cfg
    r.arb = host.install(setup.build(cfg, gpu=FakeGpu(strata, comfy)))
    r.strata.on_load = lambda: r.arb.gate("strata")
    r.run = lambda *argv: cli.main(list(argv), arbiter=r.arb, cfg=cfg)
    r.events = lambda: [json.loads(x) for x in (tmp_path / "state/vhotseat/events.jsonl").read_text().splitlines()]
    yield r
    strata.close()
    comfy.close()


# --- config ---------------------------------------------------------------------


def test_example_config_is_valid():
    cfg = config.parse((ROOT / "config.example.yaml").read_text())
    assert set(cfg["workloads"]) == {"strata", "comfy"}


@pytest.mark.parametrize("edit, where", [
    (lambda c: c["workloads"]["strata"].pop("release"), "workloads.strata.release"),
    (lambda c: c["workloads"]["strata"].update(driver="nope"), "workloads.strata.driver"),
    (lambda c: c["workloads"]["strata"].update(colour="red"), "unknown key(s) for driver http: colour"),
    (lambda c: c["workloads"]["strata"]["admission"].update(min_free_vram_mib=-1), "min_free_vram_mib"),
    (lambda c: c["workloads"]["strata"]["admission"].update(min_ram=1), "unknown key(s): min_ram"),
    (lambda c: c["workloads"]["comfy"].update(gate={"kind": "magic"}), "workloads.comfy.gate"),
    (lambda c: c["workloads"]["strata"]["release"].update(post="unload"), "must start with /"),
    (lambda c: c["workloads"]["comfy"].pop("service"), "workloads.comfy.vram"),
    (lambda c: c.update(gpu={"backend": "rocm"}), "gpu.backend"),
    (lambda c: c["workloads"].update(none=c["workloads"].pop("comfy")), "workloads.none"),
])
def test_config_errors_name_the_key(edit, where):
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    edit(raw)
    with pytest.raises(config.ConfigError, match=where.replace("(", r"\(").replace(")", r"\)")):
        config.validate(raw)


def test_bad_yaml():
    with pytest.raises(config.ConfigError, match="not valid YAML"):
        config.parse("workloads: [")


def test_state_moves_from_gpu_workload(tmp_path):
    old = tmp_path / "gpu-workload"
    old.mkdir()
    (old / "events.jsonl").write_text('{"event": "x"}\n')
    (old / "switch.lock").write_text("")
    setup.migrate_state(tmp_path / "vhotseat")
    assert (tmp_path / "vhotseat/events.jsonl").read_text() == '{"event": "x"}\n'
    assert old.is_symlink() and (old / "events.jsonl").exists()
    assert (tmp_path / "vhotseat/switch.lock").exists()


def test_lock_keeps_gpu_workloads_name(vh):
    """~/.local/state/gpu-workload/switch.lock, through the symlink: a rollback to
    gpu-workload (docs/revert-gpu-workload.md) shares the lock."""
    assert vh.arb.lock.path.name == "switch.lock"


# --- http driver -------------------------------------------------------------------


def test_lookup_paths():
    body = {"a": {"b": [{"c": 1}]}, "loaded": False}
    assert lookup(body, "a.b.0.c") == 1
    assert lookup(body, "$.a.b[0].c") == 1
    assert lookup(body, "loaded") is False
    assert lookup(body, "nope") is MISSING


def test_rules():
    assert matches({"status": 409}, 409, None)
    assert matches({"path": "status", "in": ["busy"]}, 200, {"status": "busy"})
    assert not matches({"status": 200, "path": "status", "in": ["unloaded"]}, 500, {"status": "unloaded"})
    assert matches({"path": "busy"}, 200, {"busy": True})
    assert not matches({"path": "busy"}, 200, {"busy": False})


def test_http_busy_endpoint(vh):
    """A server that says whether it's busy (`busy:`) is never released mid-work,
    even by a gate that doesn't wait."""
    w = vh.arb.workloads["strata"]
    w.spec["busy"] = {"get": "/health", "path": "loaded"}   # "busy whenever loaded", for the test
    vh.strata.loaded = True
    assert w.busy() == "Strata is busy"
    vh.comfy.guard = None
    assert vh.arb.gate("comfy") is False


# --- switch none, admission, restart ----------------------------------------------------


def test_switch_none_frees_everything(vh):
    vh.strata.loaded = True
    assert vh.run("switch", "none") == 0
    assert not vh.strata.loaded and vh.comfy.vram == 400
    vh.comfy.vram = 7000
    assert vh.run("switch", "none") == 0
    assert vh.comfy.vram == 400
    assert vh.arb.probe()["mode"] == "none"


def test_unknown_workload_is_a_usage_error(vh, capsys):
    assert vh.run("switch", "ollama") == 2
    assert "no workload named 'ollama'" in capsys.readouterr().err
    assert vh.run("gate", "ollama") == 1     # a gate must never say yes by mistake


def test_admission_restarts_comfy_for_ram(vh):
    """2026-10-16: ComfyUI under the old 4 GiB threshold, Strata loaded anyway,
    oomd killed it. Now Strata's own need decides: not enough available RAM,
    so the idle ComfyUI is restarted first."""
    vh.comfy.vram = 7000
    vh.host.mem["comfyui"] = {"ram_mib": 3000, "swap_mib": 500, "file_mib": 0}
    vh.host.available_mib = 44000
    vh.host.on_restart["comfyui"] = lambda: (vh.comfy.restarted(), setattr(vh.host, "available_mib", 47500))
    assert vh.strata.request() == 200
    assert vh.host.restarts == ["comfyui"]
    e = [e for e in vh.events() if e["event"] == "strata-load"][-1]
    assert "restarted ComfyUI to release 3500 MiB of RAM + swap for Strata" in e["reason"]


def test_admission_no_restart_when_ram_is_enough(vh):
    vh.comfy.vram = 7000
    vh.host.mem["comfyui"] = {"ram_mib": 9000, "swap_mib": 0, "file_mib": 0}
    vh.host.available_mib = 50000
    assert vh.strata.request() == 200
    assert vh.host.restarts == []      # no blanket threshold in the config: RAM is enough


def test_admission_refuses_when_restart_isnt_enough(vh):
    vh.host.available_mib = 20000      # something outside vhotseat holds it
    assert vh.strata.request() == 503
    assert vh.host.restarts == ["comfyui"]
    e = [e for e in vh.events() if e["event"] == "strata-load"][-1]
    assert not e["allowed"] and "Strata needs 46000 MiB of available RAM, 20000 MiB is available" in e["reason"]


def test_admission_vram(vh):
    vh.arb.gpu.other = 4000            # a game holds 4 GB
    assert vh.strata.request() == 503
    assert "needs 14000 MiB of free VRAM" in [e for e in vh.events() if e["event"] == "strata-load"][-1]["reason"]


def test_admission_never_restarts_a_busy_comfy(vh):
    vh.host.available_mib = 20000
    vh.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})
    time.sleep(0.05)
    assert vh.strata.request() == 503
    assert vh.host.restarts == []


def test_admission_not_applied_to_the_owner(vh):
    """`switch strata` with Strata loaded doesn't count its own 40 GB against it."""
    vh.strata.loaded = True
    vh.host.available_mib = 15000
    assert vh.run("switch", "strata") == 0
    assert vh.host.restarts == []


def test_switch_strata_checks_admission(vh):
    vh.host.available_mib = 20000
    assert vh.run("switch", "strata") == 1
    assert not vh.strata.loaded


# --- vhotseat CLI ----------------------------------------------------------------------


def test_status_names_the_owner(vh, capsys):
    vh.run("status")
    assert capsys.readouterr().out.splitlines()[0].split() == ["mode:", "none"]
    vh.strata.loaded = True
    vh.run("status", "--json")
    assert json.loads(capsys.readouterr().out)["mode"] == "strata"


def test_gate_cli(vh):
    assert vh.run("gate", "strata") == 0
    vh.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})
    time.sleep(0.05)
    assert vh.run("gate", "strata") == 1


def test_broken_config_refuses_gates(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VHOTSEAT_CONFIG", str(tmp_path / "c.yaml"))
    (tmp_path / "c.yaml").write_text("workloads: {}\n")
    assert cli.main(["gate", "strata"]) == 1
    assert cli.main(["status"]) == 2
    assert "expected at least one workload" in capsys.readouterr().err


# --- GPU attribution ---------------------------------------------------------------------


def test_attribution_by_unit_and_process():
    procs = [(1, 7000, "python"), (2, 14000, "python"), (3, 300, "Xorg"), (4, None, "python")]
    info = {1: (".../app.slice/comfyui.service", "python main.py"),
            2: (".../app.slice/strata.service", "python serve/server.py"),
            3: ("/init.scope", "/usr/lib/xorg/Xorg"), 4: ("", "")}
    snap = gpu.attribute(16303, 1000, procs,
                         {"comfy": ("unit", "comfyui.service"), "strata": ("process", "serve/server.py")},
                         procinfo=lambda pid: info[pid])
    assert snap == {"total_mib": 16303, "free_mib": 1000, "comfy_mib": 7000, "strata_mib": 14000, "other_mib": 300}


def test_nvml_matches_nvidia_smi():
    """On a machine with an NVIDIA GPU, NVML and nvidia-smi agree."""
    try:
        nvml = gpu.Nvml()
    except gpu.GpuError:
        pytest.skip("no NVML here")
    t1, _, p1 = nvml.read()
    t2, _, p2 = gpu.NvidiaSmi().read()
    assert t1 == t2
    assert {p for p, _, _ in p1} == {p for p, _, _ in p2}


# --- doctor ---------------------------------------------------------------------------------


def test_doctor_checks_gates(vh, tmp_path):
    strata_cfg = tmp_path / "strata.json"
    missing = str(tmp_path / "bin/vhotseat")
    strata_cfg.write_text(json.dumps({"before_load": [missing, "gate", "strata"], "before_load_strict": True,
                                      "before_load_timeout_s": 90}))
    nodes = tmp_path / "custom_nodes"
    (nodes / "gpu_workload_guard").mkdir(parents=True)
    (nodes / "gpu_workload_guard/__init__.py").write_text("")
    vh.arb.workloads["strata"].gate["config"] = str(strata_cfg)
    vh.arb.workloads["comfy"].gate["custom_nodes"] = str(nodes)
    results = {c: (s, d) for s, c, d in doctor.checks(vh.arb, vh.cfg)}
    assert results["comfy: gate"][0] == "ok"
    assert results["strata: gate"] == ("fail", f"before_load runs {missing}, which isn't there")
    (tmp_path / "bin").mkdir()
    Path(missing).write_text("#!/bin/sh\n")
    Path(missing).chmod(0o755)
    results = {c: (s, d) for s, c, d in doctor.checks(vh.arb, vh.cfg)}
    assert results["strata: gate"][0] == "ok"

    strata_cfg.write_text(json.dumps({"before_load": ["/bin/true"]}))
    (nodes / "gpu_workload_guard/__init__.py").unlink()
    found = [(s, c, d) for s, c, d in doctor.checks(vh.arb, vh.cfg) if "gate" in c]
    assert any(s == "fail" and "loads aren't guarded" in d for s, c, d in found)
    assert any(s == "fail" and "before_load_strict isn't true" in d for s, c, d in found)
    assert any(s == "fail" and "guard node not installed" in d for s, c, d in found)
