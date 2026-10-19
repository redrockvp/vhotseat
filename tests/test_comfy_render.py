"""comfy-render against the fake ComfyUI: batch formats, results, progress, exit codes."""

import importlib
import json
import os
import threading
import time
from pathlib import PurePath

import pytest
from conftest import cli_bin
from fakes import FakeComfy


def out_dir(comfy: str) -> str:
    """<comfy>/output/ as comfy-render prints it here (backslashes on Windows)."""
    return str(PurePath(comfy) / "output") + os.sep


@pytest.fixture
def cr(monkeypatch):
    # A fresh copy each test: comfy-render keeps its settings in module globals.
    mod = importlib.reload(importlib.import_module("vhotseat.kit.comfy_render"))
    comfy = FakeComfy()
    monkeypatch.setattr(mod, "COMFY_URL", comfy.url)
    monkeypatch.setattr(mod, "COMFY_DIR", None)
    monkeypatch.setattr(mod, "POLL_S", 0.05)
    mod.fake = comfy
    yield mod
    comfy.close()


def save(seed=1):
    return {"1": {"class_type": "KSampler", "inputs": {"seed": seed}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}}


def write(tmp_path, data):
    p = tmp_path / "jobs.json"
    p.write_text(json.dumps(data))
    return str(p)


# --- input ------------------------------------------------------------------


def test_load_one_workflow(cr, tmp_path):
    assert cr.load_jobs(write(tmp_path, save())) == [save()]


def test_load_list(cr, tmp_path):
    assert len(cr.load_jobs(write(tmp_path, [save(1), save(2)]))) == 2


def test_load_base_and_variants(cr, tmp_path):
    jobs = cr.load_jobs(write(tmp_path, {"base": save(), "jobs": [{"1.inputs.seed": 11}, {"1.inputs.seed": 22}]}))
    assert [j["1"]["inputs"]["seed"] for j in jobs] == [11, 22]


@pytest.mark.parametrize("data, msg", [
    ({"nodes": [], "links": []}, "UI-format export"),
    ({"base": save(), "jobs": [{"9.inputs.seed": 1}]}, "no node '9' in base"),
    ({"base": save(), "jobs": [{"1.seed": 1}]}, "expected NODE.inputs.NAME"),
])
def test_bad_input(cr, tmp_path, data, msg):
    with pytest.raises(cr.UsageError, match=msg):
        cr.load_jobs(write(tmp_path, data))


def test_missing_file_exits_2(cr, tmp_path, capsys):
    assert cr.main([str(tmp_path / "nope.json")]) == 2


# --- runs -------------------------------------------------------------------------


def test_batch_ok(cr, tmp_path, capsys):
    assert cr.main([write(tmp_path, [save(1), save(2)])]) == 0
    out = capsys.readouterr().out
    assert "queued 2/2 job(s)" in out
    assert f"job 1/2 ok: {out_dir('/comfy')}" in out and f"job 2/2 ok: {out_dir('/comfy')}" in out
    assert out.rstrip().endswith("2/2 job(s) succeeded, 2 file(s)")


def test_rejected_and_failed_jobs_exit_1(cr, tmp_path, capsys):
    jobs = [save(), {"1": {"class_type": "Reject", "inputs": {}}},
            {"1": {"class_type": "Fail", "inputs": {}}}]
    assert cr.main([write(tmp_path, jobs)]) == 1
    out = capsys.readouterr().out
    assert "job 1/3 ok" in out
    assert "job 2/3 rejected: node 1 Reject: Value not in list: ckpt_name: nope" in out
    assert "job 3/3 failed: node 1 Fail: boom" in out


def test_job_without_save_node_fails(cr, tmp_path, capsys):
    assert cr.main([write(tmp_path, {"1": {"class_type": "PreviewOnly", "inputs": {}}})]) == 1
    assert "saved no files" in capsys.readouterr().out


def test_guard_refusal_is_reported(cr, tmp_path, capsys):
    """When the guard can't take the GPU, the job fails with its reason."""
    cr.fake.guard = lambda pid: "Strata is answering or loading"
    assert cr.main([write(tmp_path, save())]) == 1
    assert "GPU busy, job not run: Strata is answering or loading" in capsys.readouterr().out


def test_job_deleted_from_queue_is_cancelled_exit_4(cr, tmp_path, capsys):
    cr.fake.slow_s = 1.0
    jobs = [{"1": {"class_type": "Slow", "inputs": {}}, "2": {"class_type": "SaveImage", "inputs": {}}}, save()]

    def cancel_second():
        while len(cr.fake.pending) < 1:
            time.sleep(0.01)
        cr.fake.pending.clear()  # someone deleted it from ComfyUI's queue

    threading.Thread(target=cancel_second, daemon=True).start()
    assert cr.main([write(tmp_path, jobs)]) == 4
    out = capsys.readouterr().out
    assert "job 2/2 cancelled: cancelled by the user" in out and "before it started" in out


def test_progress_lines_from_websocket(cr, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cr, "PROGRESS_EVERY_S", 0.0)
    cr.fake.slow_s = 0.6
    job = {"1": {"class_type": "Slow", "inputs": {}}, "2": {"class_type": "SaveImage", "inputs": {}}}
    assert cr.main([write(tmp_path, job)]) == 0
    out = capsys.readouterr().out
    assert "job 1/1 running: node 1 Slow" in out
    assert "step " in out and "/3 (" in out


def test_no_progress_times_out_exit_3(cr, tmp_path, capsys):
    cr.fake.guard = lambda pid: time.sleep(5)    # stuck before any node starts
    assert cr.main([write(tmp_path, save()), "--timeout", "0.5"]) == 3
    assert "no progress for 0.5s" in capsys.readouterr().out


def test_comfy_unreachable_exits_2(cr, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cr, "COMFY_URL", "http://127.0.0.1:9")
    assert cr.main([write(tmp_path, save())]) == 2
    assert "ComfyUI not reachable" in capsys.readouterr().err


def test_json_output(cr, tmp_path, capsys):
    assert cr.main([write(tmp_path, save()), "--json"]) == 0
    out = capsys.readouterr().out
    results = json.loads(out[out.index("["):])
    assert results[0]["status"] == "done" and results[0]["files"][0].startswith(out_dir("/comfy"))


def test_comfy_dir_from_comfyui(cr, tmp_path, capsys):
    """No --comfy-dir: ComfyUI's own model folder says where it lives."""
    cr.fake.models_dir = "/srv/ComfyUI/models/checkpoints"
    cr.fake.custom_nodes_dir = "/srv/ComfyUI/custom_nodes"
    assert cr.main([write(tmp_path, save())]) == 0
    assert f"job 1/1 ok: {out_dir('/srv/ComfyUI')}" in capsys.readouterr().out


def test_comfy_dir_ignores_model_folders_on_other_drives(cr, tmp_path, capsys):
    """extra_model_paths.yaml lists another drive's collection ahead of ComfyUI's own
    models, which is where the e2e test found this going wrong: the saved files still
    print under the install folder, the one that holds its custom_nodes."""
    cr.fake.extra_model_dirs = ["/mnt/media/models/checkpoints"]
    cr.fake.models_dir = "/srv/ComfyUI/models/checkpoints"
    cr.fake.custom_nodes_dir = "/srv/ComfyUI/custom_nodes"
    assert cr.main([write(tmp_path, save())]) == 0
    out = capsys.readouterr().out
    assert f"job 1/1 ok: {out_dir('/srv/ComfyUI')}" in out
    assert "/mnt/media" not in out


def test_comfy_dir_from_folder_paths_body(cr):
    install = {"checkpoints": ["/mnt/media/models/checkpoints", "/srv/ComfyUI/models/checkpoints",
                               "/srv/ComfyUI/output/checkpoints"],
               "custom_nodes": ["/srv/ComfyUI/custom_nodes"]}
    assert cr._comfy_dir_from(200, install) == PurePath("/srv/ComfyUI")
    # custom_nodes missing (an older ComfyUI): the model-folder guess still works
    assert cr._comfy_dir_from(200, {"checkpoints": ["/srv/ComfyUI/models/checkpoints"]}) == PurePath("/srv/ComfyUI")
    # a collection on another drive, and nothing that says where ComfyUI lives
    assert cr._comfy_dir_from(200, {"checkpoints": ["/mnt/media/checkpoints"]}) is None
    assert cr._comfy_dir_from(500, {}) is None


def test_comfy_dir_unknown_gives_relative_paths(cr, tmp_path, capsys):
    cr.fake.models_dir = "/somewhere/else"
    cr.fake.custom_nodes_dir = "/somewhere/else/nodes"   # not the shape <install>/custom_nodes
    assert cr.main([write(tmp_path, save())]) == 0
    assert f"job 1/1 ok: output{os.sep}" in capsys.readouterr().out


def test_comfy_dir_and_url_flags(cr, tmp_path, capsys, monkeypatch):
    url = cr.fake.url
    monkeypatch.setattr(cr, "COMFY_URL", "http://127.0.0.1:9")
    assert cr.main([write(tmp_path, save()), "--url", url, "--comfy-dir", "/opt/comfy"]) == 0
    assert f"job 1/1 ok: {out_dir('/opt/comfy')}" in capsys.readouterr().out


def test_result_survives_lost_history(cr, tmp_path, capsys):
    """ComfyUI restarted (vhotseat making room) after the job finished but before
    /history was read: the result comes from the websocket instead of being
    reported as cancelled."""
    cr.fake.lose_history = True
    assert cr.main([write(tmp_path, [save(1), save(2)])]) == 0
    out = capsys.readouterr().out
    assert f"job 1/2 ok: {out_dir('/comfy')}" in out and f"job 2/2 ok: {out_dir('/comfy')}" in out
    assert "cancelled" not in out


def test_hold_keeps_the_gpu_for_the_whole_batch(rig, tmp_path, capsys, monkeypatch):
    """--hold reruns the batch under `vhotseat hold comfy`: the LLM is unloaded
    first, and a request to it while the batch runs is refused (503) instead of
    taking the GPU back between two jobs."""
    monkeypatch.setenv("VHOTSEAT_BIN", cli_bin(tmp_path))
    monkeypatch.setenv("STRATA_URL", rig.strata.url)
    monkeypatch.setenv("COMFY_URL", rig.comfy.url)
    monkeypatch.setenv("XDG_STATE_HOME", str(rig.state.parent))
    rig.strata.loaded = True
    rig.comfy.slow_s = 1.0
    slow = {"1": {"class_type": "Slow", "inputs": {}}, "2": {"class_type": "SaveImage", "inputs": {}}}
    seen = {}

    def between():
        while not rig.comfy.pending and not rig.comfy.running:
            time.sleep(0.01)
        seen["status"] = rig.strata.request()

    t = threading.Thread(target=between, daemon=True)
    t.start()
    from vhotseat.kit import comfy_render
    code = comfy_render.main([write(tmp_path, [slow, save()]), "--hold"])
    t.join(5)
    assert code == 0
    assert seen["status"] == 503 and not rig.strata.loaded
    assert [e["event"] for e in rig.events() if e["event"].startswith("hold")] == ["hold", "hold-end"]


def test_hold_before_the_jobs_file(cr, tmp_path, monkeypatch):
    """`comfy-render --hold jobs/x.json`, as the comfyui-batching skill writes it:
    --hold's optional NAME took the file and the jobs went missing (exit 2)."""
    seen = {}
    monkeypatch.setattr(cr, "rerun_under_hold", lambda name, wait, argv: seen.update(name=name, argv=argv) or 0)
    path = write(tmp_path, save())
    assert cr.main(["--hold", path]) == 0
    assert seen == {"name": "comfy", "argv": ["--hold", path]}


def test_missing_jobs_is_a_usage_error(cr, capsys):
    with pytest.raises(SystemExit) as e:
        cr.main(["--hold"])
    assert e.value.code == 2 and "required: jobs" in capsys.readouterr().err


def test_hold_without_vhotseat_is_a_usage_error(cr, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("VHOTSEAT_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    assert cr.main([write(tmp_path, save()), "--hold"]) == 2
    assert "--hold needs the vhotseat command" in capsys.readouterr().err
