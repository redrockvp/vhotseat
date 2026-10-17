import os

import pytest

from fakes import FakeComfy, FakeGpu, FakeHost, FakeStrata
from vhotseat import compat, util


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    # Waits and polls run at a tenth of their real length.
    monkeypatch.setattr(util, "SCALE", 0.1)
    monkeypatch.delenv("GPU_WORKLOAD_HOLD", raising=False)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Fake Strata and ComfyUI, and the arbiter gpu-workload builds for them."""
    strata, comfy = FakeStrata(), FakeComfy()
    host = FakeHost()
    host.on_restart["comfyui"] = comfy.restarted
    gpu = FakeGpu(strata, comfy)
    env = {"GPU_WORKLOAD_STRATA_URL": strata.url, "GPU_WORKLOAD_COMFY_URL": comfy.url,
           "XDG_STATE_HOME": str(tmp_path)}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    arb = compat.build(env=env, gpu=gpu, host=host)

    class Rig:
        pass

    r = Rig()
    r.strata, r.comfy, r.host, r.gpu, r.arb, r.env, r.state = strata, comfy, host, gpu, arb, env, tmp_path / "gpu-workload"
    r.events = lambda: [e for e in _read(r.state / "events.jsonl")]
    r.cli = lambda *argv: compat.main(list(argv), arbiter=arb)
    yield r
    strata.close()
    comfy.close()


def _read(path):
    import json
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
