"""comfy-render inside a ticket (GPU scheduling slice 2, chunk C): `--hold` takes a ticket
(`vhotseat hold NAME --class C`), the batch runs in steps, and between steps it asks
`vhotseat should-yield`; on a yes it hands the rest of the batch back to the outer comfy-render,
which queues a new ticket for it (behind whoever was waiting) and carries on. The fake ComfyUI;
vhotseat's side is stubbed at the subprocess boundary."""

import importlib
import json

import pytest
from fakes import FakeComfy


@pytest.fixture
def cr(monkeypatch):
    mod = importlib.reload(importlib.import_module("vhotseat.kit.comfy_render"))
    comfy = FakeComfy()
    monkeypatch.setattr(mod, "COMFY_URL", comfy.url)
    monkeypatch.setattr(mod, "COMFY_DIR", None)
    monkeypatch.setattr(mod, "POLL_S", 0.05)
    monkeypatch.delenv("VHOTSEAT_TICKET", raising=False)
    monkeypatch.delenv("COMFY_RENDER_REST", raising=False)
    mod.fake = comfy
    yield mod
    comfy.close()


def save(seed=1):
    return {"1": {"class_type": "KSampler", "inputs": {"seed": seed}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}}


def write(path, data):
    path.write_text(json.dumps(data))
    return str(path)


def inside_ticket(monkeypatch, cr, tmp_path, ticket="7"):
    """As the inner comfy-render runs: under `vhotseat hold comfy` with a ticket, and a place to
    leave the rest of the batch."""
    monkeypatch.setenv("VHOTSEAT_HOLD", "comfy")
    monkeypatch.setenv("VHOTSEAT_TICKET", ticket)
    rest = tmp_path / "rest.json"
    monkeypatch.setenv("COMFY_RENDER_REST", str(rest))
    return rest


# --- steps ----------------------------------------------------------------------------------------

def test_steps_split_a_batch(cr):
    assert cr.steps([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert cr.steps([1, 2, 3], 0) == [[1, 2, 3]]          # 0: one step
    assert cr.steps([], 4) == []


# --- inside a ticket ------------------------------------------------------------------------------

def test_inside_a_ticket_the_batch_runs_in_steps_and_asks_between_them(cr, tmp_path, monkeypatch, capsys):
    inside_ticket(monkeypatch, cr, tmp_path)
    asked = []
    monkeypatch.setattr(cr, "should_yield", lambda: asked.append(1) or False)
    jobs = write(tmp_path / "jobs.json", [save(i) for i in range(5)])
    assert cr.main([jobs, "--hold", "--yield-every", "2"]) == 0
    assert len(asked) == 2                                 # after steps 1 and 2; not after the last
    out = capsys.readouterr().out
    for n in range(1, 6):
        assert f"job {n}/5 ok" in out                      # numbered across the whole batch


def test_a_yes_hands_the_rest_back_and_exits_5(cr, tmp_path, monkeypatch, capsys):
    rest = inside_ticket(monkeypatch, cr, tmp_path)
    monkeypatch.setattr(cr, "should_yield", lambda: True)
    jobs = write(tmp_path / "jobs.json", {"base": save(), "jobs": [{"1.inputs.seed": s} for s in range(5)]})
    assert cr.main([jobs, "--hold", "--yield-every", "2"]) == cr.EXIT_YIELDED == 5
    left = json.loads(rest.read_text())
    assert [w["1"]["inputs"]["seed"] for w in left] == [2, 3, 4]       # plain workflows, in order
    assert json.loads((tmp_path / "rest.json.code").read_text()) == 0   # how the done part went
    out = capsys.readouterr().out
    assert "job 2/5 ok" in out and "job 3/5" not in out
    assert "yielding the card: 3 job(s) left" in out


def test_a_failed_job_before_the_yield_is_remembered(cr, tmp_path, monkeypatch):
    rest = inside_ticket(monkeypatch, cr, tmp_path)
    monkeypatch.setattr(cr, "should_yield", lambda: True)
    bad = {"1": {"class_type": "Fail", "inputs": {}}, "2": {"class_type": "SaveImage", "inputs": {}}}
    jobs = write(tmp_path / "jobs.json", [bad, save(1), save(2)])
    assert cr.main([jobs, "--hold", "--yield-every", "2"]) == 5
    assert json.loads((tmp_path / "rest.json.code").read_text()) == cr.EXIT_FAILED
    assert len(json.loads(rest.read_text())) == 1


def test_without_a_ticket_nothing_is_asked(cr, tmp_path, monkeypatch):
    monkeypatch.setenv("VHOTSEAT_HOLD", "comfy")         # a plain hold, as before: no ticket
    monkeypatch.setattr(cr, "should_yield", lambda: pytest.fail("asked without a ticket"))
    jobs = write(tmp_path / "jobs.json", [save(i) for i in range(6)])
    assert cr.main([jobs, "--hold", "--yield-every", "2"]) == 0


def test_without_a_place_for_the_rest_it_never_yields(cr, tmp_path, monkeypatch):
    """A ticket but no COMFY_RENDER_REST (an older outer comfy-render, or `vhotseat run` typed by
    hand): yielding would drop jobs, so it runs the batch to the end."""
    inside_ticket(monkeypatch, cr, tmp_path)
    monkeypatch.delenv("COMFY_RENDER_REST")
    monkeypatch.setattr(cr, "should_yield", lambda: True)
    jobs = write(tmp_path / "jobs.json", [save(i) for i in range(3)])
    assert cr.main([jobs, "--hold", "--yield-every", "1"]) == 0


# --- should_yield ---------------------------------------------------------------------------------

def test_should_yield_asks_vhotseat_and_fails_safe(cr, monkeypatch):
    monkeypatch.setenv("VHOTSEAT_BIN", "vhotseat-test")
    calls = []

    def call(argv, **kw):
        calls.append(argv)
        return answer["code"]
    answer = {"code": 0}
    monkeypatch.setattr(cr.subprocess, "call", call)
    assert cr.should_yield() is True
    assert calls[-1][:2] == ["vhotseat-test", "should-yield"]
    answer["code"] = 1
    assert cr.should_yield() is False
    answer["code"] = 2
    assert cr.should_yield() is False

    def broken(argv, **kw):
        raise OSError("gone")
    monkeypatch.setattr(cr.subprocess, "call", broken)
    assert cr.should_yield() is False                     # can't ask: carry on, never drop work


# --- the outer comfy-render ---------------------------------------------------------------------

def test_hold_takes_a_ticket_and_requeues_the_rest(cr, tmp_path, monkeypatch):
    monkeypatch.setenv("VHOTSEAT_BIN", "vhotseat-test")
    jobs = write(tmp_path / "jobs.json", [save(i) for i in range(3)])
    seen = []

    def call(argv, env=None, **kw):
        seen.append(argv)
        rest = env["COMFY_RENDER_REST"]
        if len(seen) == 1:                                # round 1 yields with one job left
            open(rest, "w").write(json.dumps([save(9)]))
            open(rest + ".code", "w").write("1")          # and one of its jobs had failed
            return 5
        return 0
    monkeypatch.setattr(cr.subprocess, "call", call)
    code = cr.main([jobs, "--hold", "--class", "batch", "--as", "lead-38"])
    assert code == cr.EXIT_FAILED                         # the worst round decides
    assert len(seen) == 2
    first, second = seen
    assert first[:3] == ["vhotseat-test", "hold", "comfy"]
    assert "--class" in first and first[first.index("--class") + 1] == "batch"
    assert "--as" in first and first[first.index("--as") + 1] == "lead-38"
    assert jobs in first                                  # round 1 runs the caller's file
    assert jobs not in second                             # round 2 runs the rest
    inner = second[second.index("--") + 1:]               # python -m vhotseat.kit.comfy_render JOBS ...
    assert json.loads(open(inner[3]).read()) == [save(9)]


def test_hold_defaults_to_class_batch(cr, tmp_path, monkeypatch):
    monkeypatch.setenv("VHOTSEAT_BIN", "vhotseat-test")
    seen = []
    monkeypatch.setattr(cr.subprocess, "call", lambda argv, **kw: seen.append(argv) or 0)
    assert cr.main([write(tmp_path / "jobs.json", save()), "--hold"]) == 0
    assert seen[0][seen[0].index("--class") + 1] == "batch"


def test_a_round_that_ends_without_yielding_ends_the_loop(cr, tmp_path, monkeypatch):
    monkeypatch.setenv("VHOTSEAT_BIN", "vhotseat-test")
    seen = []
    monkeypatch.setattr(cr.subprocess, "call", lambda argv, **kw: seen.append(argv) or 3)
    assert cr.main([write(tmp_path / "jobs.json", save()), "--hold"]) == 3   # a timeout, passed on
    assert len(seen) == 1
