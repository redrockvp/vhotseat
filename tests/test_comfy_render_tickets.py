"""comfy-render inside a ticket (GPU scheduling slice 2, chunk C): `--hold` takes a ticket
(`vhotseat hold NAME --class C`), the batch runs in steps, and between steps it asks
`vhotseat should-yield`; on a yes it hands the rest of the batch back to the outer comfy-render,
which queues a new ticket for it (behind whoever was waiting) and carries on. The fake ComfyUI;
vhotseat's side is stubbed at the subprocess boundary."""

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

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


# Run signals in a child so the default SIGTERM action cannot kill pytest.
# The first three submissions are real HTTP requests to FakeComfy. Crash on the
# fourth submission, or signal the renderer during history polling.
_ABORT_BATCH = """
import signal, sys
from vhotseat.kit import comfy_render as cr
reason = sys.argv.pop(1)
request = cr._request
submitted = 0
def inject(method, path, *args, **kwargs):
    global submitted
    if method == 'POST' and path == '/prompt':
        if reason == 'exception' and submitted == 3:
            raise RuntimeError('simulated submission failure')
        submitted += 1
    if reason.startswith('SIG') and path.startswith('/history/'):
        signum = getattr(signal, reason)
        # Deliver once, even if cleanup itself reads history.
        cr._request = request
        signal.raise_signal(signum)
    return request(method, path, *args, **kwargs)
cr._request = inject
cr.POLL_S = 0.02
sys.exit(cr.main())
"""


@pytest.mark.parametrize("reason", ["exception", "SIGINT", "SIGTERM", "timeout"]
                         + (["SIGBREAK"] if sys.platform == "win32" else []))
def test_abort_inside_hold_cleans_only_owned_pending(cr, tmp_path, monkeypatch, reason):
    inside_ticket(monkeypatch, cr, tmp_path)
    fake = cr.fake
    release = threading.Event()
    foreign = []

    def guard(pid):
        if not foreign:
            foreign.append(fake.queue_job())   # Peter submits while our first job runs
            if reason != "timeout":
                fake.queue_posted.wait(50)     # finish shortly after the pending jobs are removed
                return
        release.wait(50)                       # stuck render, or Peter's job after ours

    fake.guard = guard
    n = 4 if reason == "exception" else 3
    jobs = write(tmp_path / "jobs.json", [save(i) for i in range(n)])
    try:
        child = subprocess.run(
            [sys.executable, "-c", _ABORT_BATCH, reason, jobs, "--url", fake.url,
             "--hold", "comfy", "--timeout", "0.2" if reason == "timeout" else "30"],
            capture_output=True, timeout=45,
        )
        assert len(fake.clients) == 3, child.stderr
        running, *pending = fake.clients  # insertion order of accepted submissions
        if reason == "timeout":
            assert child.returncode == cr.EXIT_TIMEOUT
            assert fake.running == [running]   # the bounded wait expired with it still running
        else:
            assert child.returncode != 0
        assert {pid for post in fake.queue_posts for pid in post["delete"]} == set(pending)
        assert set(pending).isdisjoint(fake.pending + fake.running + list(fake.history))
        assert foreign and foreign[0] in fake.pending + fake.running
        assert "POST /interrupt" not in fake.calls
        if reason != "timeout":
            assert fake.history[running]["status"]["completed"]  # finished before leaving the hold
    finally:
        release.set()
        fake.queue_posted.set()


def test_stdout_preserves_utf8_path_on_cp1252(cr, tmp_path):
    jobs = write(tmp_path / "jobs.json", save())
    entry = Path(__file__).resolve().parents[1] / "comfy-render"
    child = subprocess.run(
        [sys.executable, str(entry), jobs, "--url", cr.fake.url, "--comfy-dir", "東京"],
        env={**os.environ, "PYTHONIOENCODING": "cp1252:strict"},
        capture_output=True, timeout=30,
    )
    assert child.returncode == 0, child.stderr
    assert "東京".encode("utf-8") in child.stdout


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
    seen, ran = [], []

    def call(argv, env=None, **kw):
        seen.append(argv)
        inner = argv[argv.index("--") + 1:]               # python -m vhotseat.kit.comfy_render JOBS ...
        jobs_now = env.get("COMFY_RENDER_JOBS") or inner[3]   # a later round gets the rest by env
        ran.append(json.loads(open(jobs_now).read()))      # what this round runs, read while it runs
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
    assert ran == [[save(i) for i in range(3)], [save(9)]]


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


def _yielding_rounds(cr, tmp_path, monkeypatch, rounds):
    """Stub vhotseat: rounds 1..rounds-1 yield, each leaving one more job; the last ends."""
    monkeypatch.setenv("VHOTSEAT_BIN", "vhotseat-test")
    seen = []

    def call(argv, env=None, **kw):
        inner = argv[argv.index("--") + 1:]
        jobs = env.get("COMFY_RENDER_JOBS") or next(a for a in inner[3:] if a.endswith(".json"))
        seen.append(json.loads(open(jobs).read()))
        if len(seen) < rounds:
            rest = env["COMFY_RENDER_REST"]
            open(rest, "w").write(json.dumps([save(100 + len(seen))]))
            open(rest + ".code", "w").write("0")
            return 5
        return 0
    monkeypatch.setattr(cr.subprocess, "call", call)
    return seen


def test_every_round_runs_the_previous_rounds_rest(cr, tmp_path, monkeypatch):
    seen = _yielding_rounds(cr, tmp_path, monkeypatch, rounds=3)
    assert cr.main([write(tmp_path / "jobs.json", [save(1), save(2)]), "--hold"]) == 0
    assert seen == [[save(1), save(2)], [save(101)], [save(102)]]      # never a batch twice


def test_the_skills_hold_before_the_file_form_requeues_too(cr, tmp_path, monkeypatch):
    """`comfy-render --hold jobs/x.json`, as the comfyui-batching skill writes it."""
    seen = _yielding_rounds(cr, tmp_path, monkeypatch, rounds=3)
    assert cr.main(["--hold", write(tmp_path / "jobs.json", [save(1)])]) == 0
    assert seen == [[save(1)], [save(101)], [save(102)]]


def test_rest_files_are_cleaned_up(cr, tmp_path, monkeypatch):
    made = []
    real = cr.tempfile.mkdtemp
    monkeypatch.setattr(cr.tempfile, "mkdtemp", lambda **kw: made.append(real(**kw)) or made[-1])
    _yielding_rounds(cr, tmp_path, monkeypatch, rounds=3)
    assert cr.main([write(tmp_path / "jobs.json", [save(1)]), "--hold"]) == 0
    import os
    assert made and not os.path.exists(made[0])


def test_json_output_is_one_document_when_stepping(cr, tmp_path, monkeypatch, capsys):
    inside_ticket(monkeypatch, cr, tmp_path)
    monkeypatch.setattr(cr, "should_yield", lambda: False)
    jobs = write(tmp_path / "jobs.json", [save(i) for i in range(5)])
    assert cr.main([jobs, "--hold", "--yield-every", "2", "--json"]) == 0
    out = capsys.readouterr().out
    doc = json.loads(out[out.index("["):])                 # progress lines first, then one array
    assert [r["job"] for r in doc] == [1, 2, 3, 4, 5]
