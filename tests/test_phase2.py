"""Phase 2: `doctor --exercise`, the patches registry."""

import json
from pathlib import Path

from vhotseat import config, doctor

ROOT = Path(__file__).resolve().parent.parent


# --- doctor --exercise ----------------------------------------------------------------


def test_exercise_strata(rig, capsys):
    """A full cycle: ComfyUI released, Strata loaded and seen as resident, then
    released and its VRAM gone. The GPU ends free."""
    rig.comfy.vram = 7000
    assert rig.cli("doctor", "--exercise", "strata") == 0
    out = capsys.readouterr().out
    for step in ("take the GPU", "load", "resident", "release", "released"):
        assert f"ok    strata: {step} (" in out
    assert "freed its 7000 MiB of models" in out
    assert not rig.strata.loaded and rig.arb.probe()["mode"] == "none"
    ev = [e for e in rig.events() if e["event"] == "exercise"][-1]
    assert ev["ok"] and [s["step"] for s in ev["steps"]][:2] == ["take the GPU", "load"]


def test_exercise_comfy_skips_load(rig, capsys):
    """ComfyUI loads on its first job, so its cycle is take + release."""
    rig.strata.loaded = True
    rig.comfy.vram = 7000
    assert rig.cli("doctor", "--exercise", "comfy", "--json") == 0
    steps = {s["step"]: s for s in json.loads(capsys.readouterr().out)}
    assert "skipped" in steps["load"]["detail"]
    assert steps["release"]["status"] == "ok" and rig.comfy.vram == 400
    assert not rig.strata.loaded


def test_exercise_catches_a_release_that_leaves_vram(rig, capsys):
    """What the exercise is for: a driver (or an extension) whose release says ok
    but leaves the model on the GPU."""
    rig.comfy.vram = 7000
    rig.comfy.stuck_vram = 2066
    rig.arb.workloads["comfy"].allow_restart = False
    assert rig.cli("doctor", "--exercise", "comfy") == 1
    assert "FAIL  comfy: release" in capsys.readouterr().out


def test_exercise_catches_a_resident_probe_that_lies(rig, capsys):
    """A `resident:` that never says loaded, on a workload whose VRAM can't be told
    apart from other processes': nothing would ever release it."""
    w = rig.arb.workloads["strata"]
    w.spec["resident"] = {"get": "/health", "path": "nope"}
    rig.gpu.strata_as_other = True
    assert rig.cli("doctor", "--exercise", "strata") == 1
    assert "FAIL  strata: resident" in capsys.readouterr().out


def test_exercise_refuses_while_busy(rig, capsys):
    rig.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})
    import time
    time.sleep(0.05)
    assert rig.cli("doctor", "--exercise", "comfy") == 1
    assert "FAIL  comfy: idle" in capsys.readouterr().out


def test_exercise_unknown_workload(rig, capsys):
    assert rig.cli("doctor", "--exercise", "ollama") == 2


# --- patches ------------------------------------------------------------------------------


def _cfg(tmp_path, *, marker_in_file=True, installed="5.10.2", fixed="5.10.4"):
    src = tmp_path / "infer.py"
    src.write_text("def to(self):\n" + ("    # LOCAL PATCH (vhotseat)\n" if marker_in_file else ""))
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{installed}"\n')
    entry = {"name": "IndexTTS unload", "file": str(src), "marker": "LOCAL PATCH (vhotseat)", "doc": "README"}
    if fixed:
        entry["fixed_upstream_in"] = {"version": fixed, "version_file": str(tmp_path / "pyproject.toml")}
    return {"patches": [entry]}


def test_patch_applied(tmp_path):
    [(status, check, detail)] = doctor.check_patches(_cfg(tmp_path))
    assert status == "ok" and check == "patch: IndexTTS unload"
    assert "upstream has it from 5.10.4, installed 5.10.2" in detail


def test_patch_dropped_by_an_update(tmp_path):
    [(status, _, detail)] = doctor.check_patches(_cfg(tmp_path, marker_in_file=False, fixed=None))
    assert status == "fail" and "an update dropped it; re-apply it; see README" in detail


def test_patch_superseded_upstream(tmp_path):
    [(status, _, detail)] = doctor.check_patches(_cfg(tmp_path, marker_in_file=False, installed="5.10.4"))
    assert status == "ok" and "remove this entry" in detail


def test_patch_still_applied_after_upstream_fix(tmp_path):
    [(status, _, detail)] = doctor.check_patches(_cfg(tmp_path, installed="5.11.0"))
    assert status == "warn" and "the local patch can go" in detail


def test_patches_config_is_validated():
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    raw["patches"] = [{"name": "x", "file": "/tmp/x"}]
    import pytest
    with pytest.raises(config.ConfigError, match=r"patches\[0\].marker"):
        config.validate(raw)


def test_example_config_parses_with_every_section():
    """The live config is a copy of this: it must always validate."""
    cfg = config.parse((ROOT / "config.example.yaml").read_text())
    assert {"gpu", "workloads", "patches", "events"} <= set(cfg)


def test_proxy_guide_config_validates():
    """docs/guides/proxy-gate.md's config block is valid: an agent copies it."""
    import yaml
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    guide = (ROOT / "docs/guides/proxy-gate.md").read_text()
    block = guide.split("```yaml\n", 1)[1].split("```", 1)[0]
    raw["workloads"].update(yaml.safe_load(block)["workloads"])
    config.validate(raw)
