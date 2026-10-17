"""comfy-render against the fake ComfyUI: batch formats, results, progress, exit codes."""

import importlib.machinery
import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

from fakes import FakeComfy

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cr(monkeypatch):
    loader = importlib.machinery.SourceFileLoader("comfy_render", str(ROOT / "comfy-render"))
    spec = importlib.util.spec_from_loader("comfy_render", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    comfy = FakeComfy()
    monkeypatch.setattr(mod, "COMFY_URL", comfy.url)
    monkeypatch.setattr(mod, "COMFY_DIR", Path("/comfy"))
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
    assert "job 1/2 ok: /comfy/output/" in out and "job 2/2 ok: /comfy/output/" in out
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
    assert results[0]["status"] == "done" and results[0]["files"][0].startswith("/comfy/output/")
