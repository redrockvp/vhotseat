import json
from pathlib import Path

import pytest

from fakes import FakeComfy, FakeGpu, FakeHost, FakeStrata
from vhotseat import cli, config, setup, util

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    # Waits and polls run at a tenth of their real length.
    monkeypatch.setattr(util, "SCALE", 0.1)
    monkeypatch.delenv("VHOTSEAT_HOLD", raising=False)


def example_config(strata_url: str, comfy_url: str) -> dict:
    """config.example.yaml, pointed at the fakes."""
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    raw["workloads"]["strata"]["url"] = strata_url
    raw["workloads"]["comfy"]["url"] = comfy_url
    return config.validate(raw)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Fake Strata and ComfyUI, and vhotseat built from config.example.yaml for them.
    Strata's before_load runs the arbiter's gate, as strata-*.json does."""
    strata, comfy, host = FakeStrata(), FakeComfy(), FakeHost()
    host.on_restart["comfyui"] = comfy.restarted
    gpu = FakeGpu(strata, comfy)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = example_config(strata.url, comfy.url)
    arb = host.install(setup.build(cfg, gpu=gpu))
    strata.on_load = lambda: arb.gate("strata")

    class Rig:
        pass

    r = Rig()
    r.strata, r.comfy, r.host, r.gpu, r.arb, r.cfg = strata, comfy, host, gpu, arb, cfg
    r.state = tmp_path / "state" / "vhotseat"
    r.events = lambda: [json.loads(x) for x in (r.state / "events.jsonl").read_text().splitlines()] \
        if (r.state / "events.jsonl").exists() else []
    r.cli = lambda *argv: cli.main(list(argv), arbiter=arb, cfg=cfg)
    yield r
    strata.close()
    comfy.close()
