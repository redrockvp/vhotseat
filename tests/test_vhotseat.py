"""Phase 1: the config file, the http driver, admission, `switch none`, the
`vhotseat` CLI, GPU attribution and doctor."""

import json
import os
import threading
import time
from pathlib import Path

import pytest
from fakes import FakeComfy, FakeGpu, FakeHost, FakeStrata

from vhotseat import cli, config, doctor, gpu, setup
from vhotseat.drivers.http import MISSING, lookup, matches

ROOT = Path(__file__).resolve().parent.parent


def config_for(strata_url, comfy_url, **strata_extra):
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    raw["workloads"]["strata"]["url"] = strata_url
    raw["workloads"]["comfy"]["url"] = comfy_url
    raw["workloads"]["strata"].update(strata_extra)
    raw["claims"], raw["claims_builtin"] = [], False      # see conftest.example_config
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


def test_a_config_that_isnt_utf8(tmp_path):
    """`test_bad_yaml`'s neighbour one level down in the file: an editor that saved it as
    Windows-1252 - where an en dash in a comment is one byte, and that byte isn't a UTF-8 sequence -
    leaves a file that exists and isn't text. Every command reads the config through `load`, and a
    decode error that isn't a ConfigError is a traceback where the owner should have been told which
    file and what's wrong with it."""
    path = tmp_path / "config.yaml"
    raw = (ROOT / "config.example.yaml").read_bytes()
    assert b"# priority" in raw
    path.write_bytes(raw.replace(b"# priority", b"# \x96 priority", 1))
    with pytest.raises(config.ConfigError, match="not utf-8 text"):
        config.load(path)


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
    gpu-workload shares the lock."""
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
    assert not e["allowed"] and "Strata (qwen3.8-flash-next-iq3_xxs) needs 46000 MiB of available RAM, 20000 MiB is available" in e["reason"]


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


def test_attribution_falls_back_to_the_process_name():
    """A process whose command line vhotseat may not read (it runs elevated) still gets
    attributed by the name the GPU reader reports."""
    procs = [(7, 20000, "strata.exe"), (8, 300, "python.exe")]
    snap = gpu.attribute(24576, 600, procs,
                         {"strata": ("process", "strata.exe"), "comfy": ("process", "comfy/main.py")},
                         procinfo=lambda pid: ("", ""))
    assert snap == {"total_mib": 24576, "free_mib": 600, "strata_mib": 20000, "comfy_mib": 0, "other_mib": 300}


def test_holders_names_who_holds_the_card():
    """The sums say `other 4200`; the answer to "what is on my card" is a name."""
    procs = [(7, 20000, "strata.exe"), (8, 3000, "python.exe"), (9, 1200, "Xorg")]
    sel = {"strata": ("process", "strata.exe")}
    rows = gpu.holders(24576, 600, procs, sel, procinfo=lambda pid: ("", ""))
    assert [(r["name"], r["mib"], r["workload"]) for r in rows] == [
        ("strata.exe", 20000, "strata"), ("python.exe", 3000, None), ("Xorg", 1200, None)]
    assert [r["name"] for r in gpu.holders(24576, 600, procs, sel, procinfo=lambda p: ("", ""), limit=1)] == ["strata.exe"]


def test_nvml_matches_nvidia_smi():
    """On a machine with an NVIDIA GPU, NVML and nvidia-smi agree."""
    try:
        nvml = gpu.Nvml()
    except gpu.GpuError:
        pytest.skip("no NVML here")
    t1, _, p1 = nvml.read()
    t2, _, p2 = gpu.NvidiaSmi().read()
    assert t1 == t2
    if os.name == "nt":
        # WDDM: NVML also lists the compositor and the like, which nvidia-smi leaves out.
        assert {p for p, _, _ in p2} <= {p for p, _, _ in p1}
    else:
        assert {p for p, _, _ in p1} == {p for p, _, _ in p2}


# --- doctor ---------------------------------------------------------------------------------


def test_doctor_checks_gates(vh, tmp_path):
    strata_cfg = tmp_path / "strata.json"
    missing = str(tmp_path / "bin" / ("vhotseat.exe" if os.name == "nt" else "vhotseat"))
    strata_cfg.write_text(json.dumps({"before_load": [missing, "gate", "strata"], "before_load_strict": True,
                                      "before_load_timeout_s": 90}))
    nodes = tmp_path / "custom_nodes"
    (nodes / "vhotseat_guard").mkdir(parents=True)
    (nodes / "vhotseat_guard/__init__.py").write_text("")
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
    (nodes / "vhotseat_guard/__init__.py").unlink()
    found = [(s, c, d) for s, c, d in doctor.checks(vh.arb, vh.cfg) if "gate" in c]
    assert any(s == "fail" and "loads aren't guarded" in d for s, c, d in found)
    assert any(s == "fail" and "before_load_strict isn't true" in d for s, c, d in found)
    assert any(s == "fail" and "guard node not installed" in d for s, c, d in found)


# --- admission per model -------------------------------------------------------------


def test_admission_follows_the_model(vh):
    """IQ2 needs less RAM than IQ3: 43 GB available is enough for one, not the other."""
    vh.host.available_mib = 43000
    vh.strata.model = "qwen3.8-flash-next-iq2_xs"
    assert vh.strata.request() == 200
    vh.strata.loaded = False
    vh.strata.model = "qwen3.8-flash-next-iq3_xxs"
    assert vh.strata.request() == 503
    e = [e for e in vh.events() if e["event"] == "strata-load"][-1]
    assert "Strata (qwen3.8-flash-next-iq3_xxs) needs 46000 MiB of available RAM" in e["reason"]


def test_unlisted_model_gets_the_default(vh):
    vh.host.available_mib = 43000
    vh.strata.model = "qwen3.8-flash-next-q4"
    assert vh.strata.request() == 503
    assert "(qwen3.8-flash-next-q4) needs 46000 MiB" in [e for e in vh.events() if e["event"] == "strata-load"][-1]["reason"]


def test_by_model_keeps_the_default_vram(vh):
    adm, model = vh.arb.workloads["strata"].admission_for()
    assert model == "qwen3.8-flash-next-iq3_xxs"
    assert adm == {"min_available_ram_mib": 46000, "min_free_vram_mib": 14000}


def test_by_model_needs_a_model_request():
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    del raw["workloads"]["strata"]["model"]
    with pytest.raises(config.ConfigError, match="admission.by_model: needs `model:`"):
        config.validate(raw)


def test_doctor_names_the_models_admission(vh):
    results = {c: (s, d) for s, c, d in doctor.checks(vh.arb, vh.cfg) if c == "strata: admission"}
    assert results["strata: admission"][0] == "ok"
    assert "qwen3.8-flash-next-iq3_xxs" in results["strata: admission"][1]
    vh.strata.model = "something-new"
    found = [(s, d) for s, c, d in doctor.checks(vh.arb, vh.cfg) if c == "strata: admission"]
    assert found == [("warn", "serves something-new, which isn't in by_model; using the defaults")]


def test_gate_doesnt_count_a_loaded_workloads_own_memory(vh):
    """`vhotseat gate strata` with Strata already loaded (42 GB of RAM its own):
    nothing to make room for, so no ComfyUI restart (seen 2026-10-17, run by hand)."""
    vh.strata.loaded = True
    vh.host.available_mib = 14000
    assert vh.arb.gate("strata") is True
    assert vh.host.restarts == []


def test_a_workload_that_needs_a_key_says_so_rather_than_looking_down(vh, monkeypatch):
    """A 401 is not "the server is down": it is answering and refusing. Blurred together,
    a key that rotated sends every handover off hunting for a dead port."""
    w = vh.arb.workloads["strata"]
    w.spec["headers"] = {"Authorization": "Bearer ${VH_TEST_KEY}"}
    vh.strata.want_header = "Bearer sekrit"
    monkeypatch.delenv("VH_TEST_KEY", raising=False)
    said = w.describe(vh.arb.probe()["strata"])
    assert "isn't being sent" in said and "VH_TEST_KEY" in said
    monkeypatch.setenv("VH_TEST_KEY", "sekrit")
    st = vh.arb.probe()["strata"]
    assert st["reachable"] is True and st["loaded"] is False
    assert vh.strata.last_headers.get("authorization") == "Bearer sekrit"
    # doctor must say the same thing rather than calling it down.
    from vhotseat import doctor
    monkeypatch.delenv("VH_TEST_KEY", raising=False)
    said = [d for lvl, key, d in doctor.checks(vh.arb, vh.cfg) if key == "strata: api"]
    assert said and "wants a key" in said[0] and "VH_TEST_KEY" in said[0]


def test_an_empty_loaded_list_does_not_mean_a_model_is_loaded(vh):
    """`data.0` in {"data": []} is nothing. The absent-path sentinel is a truthy object, so
    reading it as a value made release() insist a model was loaded but unnameable, and every
    handover away from that workload failed."""
    w = vh.arb.workloads["strata"]
    w.spec["resident"] = {"get": "/loaded-list", "path": "data.0"}
    w.spec["model"] = {"get": "/loaded-list", "path": "data.0.id"}
    w.spec["release"]["body"] = {"model_path": "{model}"}
    result = vh.arb.switch("comfy", 0)
    assert result["ok"] is True, result.get("error")
    vh.strata.loaded = True
    assert vh.arb.probe()["strata"]["loaded"] is True
    assert vh.arb.switch("strata", 0)["ok"] is True
    assert vh.arb.switch("comfy", 0)["ok"] is True


def test_an_estimate_body_field_with_no_placeholder_is_sent_as_is(vh):
    """A constant in the estimate body - quant: "Q4_K_M" - carries no {placeholder}, so the
    single-placeholder check indexed names[0] of an empty list: IndexError inside probe(),
    which takes status, switch, gate and the dashboard down with it."""
    w = vh.arb.workloads["strata"]
    w.spec["estimate"] = {"from": {"get": "/last-model"}, "post": "/estimate",
                          "body": {"model_path": "{id}", "quant": "Q4_K_M"}, "path": "gpu_bytes"}
    st = vh.arb.probe()["strata"]
    assert st["sized"]["bytes"] == 19 * 2**30
    assert vh.strata.last_body == {"model_path": vh.strata.model, "quant": "Q4_K_M"}


def test_a_server_that_is_still_starting_is_not_read_as_closed(vh):
    """The other half of "a closed workload must not block the others": a server that is up but
    not answering yet - llama-server returns 503 from /health while it loads - holds its port.
    Waiting is right; calling it unreachable lets the target load on top of it, or fails the
    switch with advice to start a server that is already starting."""
    vh.strata.loaded = True
    vh.strata.down = True                          # listening, answering 503: still loading
    threading.Timer(0.4, lambda: setattr(vh.strata, "down", False)).start()
    result = vh.arb.switch("comfy", 5)
    assert result["ok"] is True, result.get("error")
    assert vh.strata.loaded is False               # released, not walked over


def test_a_workload_that_is_really_closed_still_does_not_block(vh):
    """And the half that must not regress: nothing listening, nothing on the card - it is off,
    and a handover must not wait on a port nobody owns or complain that it can't start it."""
    vh.strata.close()
    result = vh.arb.switch("comfy", 5)
    assert result["ok"] is True, result.get("error")
    assert vh.host.starts == []                    # nothing was started on the way
    assert "cannot start it" not in str(result.get("error") or "")


def test_a_failed_sizing_answer_is_not_asked_again_every_probe(vh):
    """A failed answer is still an answer. Nothing was cached for it, so every probe paid the
    `from:` round trip again - on Windows, seconds of it, per probe, for a server that is down."""
    w = vh.arb.workloads["strata"]
    w.spec["estimate"] = {"from": {"get": "/last-model"}, "post": "/estimate",
                          "body": {"model_path": "{id}"}, "path": "gpu_bytes"}
    vh.strata.down = True
    for _ in range(3):
        vh.arb.probe()
    assert vh.strata.calls.count("GET /last-model") == 1, vh.strata.calls


def test_a_workload_is_never_refused_by_its_own_floor(vh):
    """min_free_vram_mib is what a model needs room to load into. Weights already on the card
    are not room it still has to find: asking for the card again while they land must not be
    refused by the memory its own load put there."""
    assert vh.arb.workloads["strata"].admission_for()[0]["min_free_vram_mib"] == 14000
    vh.strata.loaded = True                    # someone loaded it; nobody asked first
    result = vh.arb.switch("strata", 0)
    assert result["ok"] is True, result.get("error")


def test_a_workload_can_size_a_model_before_it_lands(vh):
    """Ask the server what one of its models would cost on the card. Reported, never
    enforced: it may still fit it by shrinking the context or spilling into RAM, and the
    number is what makes that visible before the weights land rather than after."""
    w = vh.arb.workloads["strata"]
    w.spec["estimate"] = {"from": {"get": "/last-model"}, "post": "/estimate",
                          "body": {"model_path": "{id}", "gguf_variant": "{gguf_variant}",
                                   "n_ctx": "{ctx}"},
                          "ctx": 32768, "path": "gpu_bytes"}
    st = vh.arb.probe()["strata"]
    assert st["sized"]["bytes"] == 19 * 2**30
    assert vh.strata.last_body == {"model_path": vh.strata.model, "gguf_variant": "Q4_K_XL",
                                   "n_ctx": 32768}          # a number, not "32768"
    vh.arb.probe()
    assert vh.strata.estimate_calls == 1                    # the sampler doesn't pay per probe
    assert "sizes at 19456 MiB" in w.describe(st)
    assert any("sizes at 19456 MiB at 32768 ctx" in n for n in vh.arb.why_all()["strata"]["notes"])


def test_restarting_a_workload_that_wants_a_key_does_not_wait_on_its_auth(vh, monkeypatch):
    """The restart and start loops polled with a bare GET, so the workload's own `headers:`
    were never sent: a keyed API answered 401 forever, the loop waited out its whole timeout,
    and the switch failed over a server that was up the entire time."""
    w = vh.arb.workloads["strata"]
    w.spec["headers"] = {"Authorization": "Bearer ${VH_TEST_KEY}"}
    vh.strata.want_header = "Bearer sekrit"        # 401 to everything else, /health included
    monkeypatch.delenv("VH_TEST_KEY", raising=False)
    w.restart_timeout_s = 2.0
    started = time.monotonic()
    w.restart("its RAM is pinned")                 # used to wait the full 2 s and raise
    assert vh.host.restarts, vh.host.restarts
    assert time.monotonic() - started < 1.5


def test_a_release_can_name_the_model_it_is_unloading(vh):
    """Some servers unload one model at a time, by name. {model} comes from `model:`, so
    the release says which model it means instead of guessing."""
    w = vh.arb.workloads["strata"]
    w.spec["release"]["body"] = {"model_path": "{model}"}
    assert vh.arb.switch("comfy", 0)["ok"] is True
    assert vh.strata.last_body == {"model_path": vh.strata.model}


def test_an_id_naming_release_is_skipped_when_nothing_is_loaded(vh):
    """Servers that unload one model by id (Unsloth does) make an empty id either an error
    or a lie that nothing happened. With nothing loaded, the ask is skipped instead, so a
    handover through that workload doesn't die on a 422."""
    w = vh.arb.workloads["strata"]
    w.spec["model"] = {"get": "/health", "path": "model"}
    w.spec["release"]["body"] = {"model_path": "{model}"}
    vh.strata.loaded, vh.strata.model = False, ""
    assert vh.arb.switch("comfy", 0)["ok"] is True
    assert "POST /unload" not in vh.strata.calls


def test_header_values_are_validated(vh):
    """A header that isn't a string, or carries a newline, would break every request that
    workload makes - and fail as a confusing HTTP error rather than a config error."""
    import pytest as _pt
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    raw["workloads"]["strata"]["headers"] = {"Authorization": 42}
    with _pt.raises(config.ConfigError, match=r"headers"):
        config.validate(raw)
    raw["workloads"]["strata"]["headers"] = {"Authorization": "Bearer a\r\nX-Injected: 1"}
    with _pt.raises(config.ConfigError, match=r"newline"):
        config.validate(raw)


def test_cli_why_reads_the_handover_decision(vh, capsys):
    """`vhotseat why` is the dry run for when a job won't start: the same preconditions
    `switch` checks, read off the machine, releasing nothing."""
    vh.strata.loaded = True
    assert vh.run("why", "comfy") == 0
    out = capsys.readouterr().out
    assert "ComfyUI:" in out and "could take the card now" in out
    assert "Strata holds 14000 MiB and would be asked to release it" in out
    assert out.count("could take the card now") == 1        # one workload, not both
    assert vh.run("why", "--json") == 0
    why = json.loads(capsys.readouterr().out)
    assert why["strata"]["would"] == "ready" and why["strata"]["holder"] == "strata"
    assert vh.run("why", "nosuch") == 2                    # still a workload name check
