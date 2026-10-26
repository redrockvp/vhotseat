"""vhotseat's behaviour on the Strata + ComfyUI setup (config.example.yaml),
end to end against fake Strata and ComfyUI servers.

Each race and deadlock in docs/how-it-works.md §3-4 is a scenario here, as are
the ComfyUI restart cases from the README (tested by hand on 2026-10-16).
"""

import json
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import cli_bin
from fakes import COMFY_CONTEXT_VRAM

from vhotseat import claims, host

ROOT = Path(__file__).resolve().parent.parent


def last(r, kind=None):
    evs = [e for e in r.events() if kind is None or e["event"] == kind]
    return evs[-1] if evs else None


# --- status ------------------------------------------------------------------


def test_status_modes(rig, capsys):
    assert rig.arb.probe()["mode"] == "none"
    rig.strata.loaded = True
    assert rig.arb.probe()["mode"] == "strata"
    rig.comfy.vram = 7000
    assert rig.arb.probe()["mode"] == "conflict"


def test_status_json_shape_is_unchanged(rig, capsys):
    """A dashboard reads these keys: they are the --json contract."""
    assert rig.cli("status", "--json") == 0
    s = json.loads(capsys.readouterr().out)
    assert set(s) == {"mode", "strata", "comfy", "gpu", "holders", "claims", "policy",
                      "pressure", "ram", "last_event", "last_used", "asks", "notifications"}
    assert set(s["strata"]) == {"service", "reachable", "loaded"}
    assert set(s["policy"]["strata"]) == {"priority", "yields_to", "gives_way_for"}
    # What an Ask me is decided with: when each workload was last seen working (no stamp is said
    # as no stamp), which questions are open, and whether this box can notify anybody at all.
    assert set(s["last_used"]) == {"strata", "comfy"}
    assert s["last_used"]["strata"] == {"epoch": None, "ago_s": None,
                                        "phrase": "nothing has recorded it working since this started"}
    assert s["asks"] == []
    assert set(s["notifications"]) == {"available", "how", "why"}
    # What the pressure trigger is reading, whether or not anything is happening: the thresholds,
    # the same numbers the card tiles show, and the open and last episode (both empty here).
    pr = s["pressure"]
    assert {"enabled", "min_free_mib", "grow_mib", "sustain_s", "headroom_mib",
            "end_margin_mib", "cooldown_s", "ignore", "would", "why", "open", "last",
            "free_mib", "total_mib", "other_mib", "floor_mib"} <= set(pr)
    assert pr["open"] is None and pr["last"] is None and pr["would"] is False
    assert set(s["comfy"]) == {"service", "reachable", "running", "pending", "holds_models"}
    assert set(s["gpu"]) == {"total_mib", "free_mib", "strata_mib", "comfy_mib", "other_mib",
                             "shared_mib"}
    assert set(s["holders"][0]) == {"pid", "mib", "name", "workload"}
    assert set(s["ram"]) >= {"total_mib", "available_mib", "strata", "comfy"}


def test_status_text(rig, capsys):
    rig.strata.loaded = True
    rig.cli("status")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "mode:   strata"
    assert out[1] == "strata: model loaded"
    assert out[2] == "comfy:  no models loaded, 0 running / 0 queued"
    # who may be unloaded for whom, next to the memory that decision covers
    assert out[3] == "policy: strata 50 (anything); comfy 50 (anything)"
    assert out[4].startswith("gpu:    1903 of 16303 MiB free (strata 14000, comfy 400, other 0)")
    # who actually holds it, by name: the workloads and anything nothing manages
    assert out[5] == "holds:  strata.exe 14000 (strata)  python.exe 400 (comfy)"


# --- switch --------------------------------------------------------------------


def test_switch_strata_frees_comfy_then_loads(rig):
    rig.comfy.vram = 7000
    assert rig.cli("switch", "strata") == 0
    assert rig.strata.loaded and rig.comfy.vram == 400
    assert "POST /free" in rig.comfy.calls
    assert last(rig, "switch")["to"] == "strata"
    # Strata's own hook ran during /load and allowed it: the switch asked for this load.
    assert last(rig, "strata-load")["allowed"] is True
    assert last(rig, "strata-load")["reason"] == "switch strata in progress"


def test_switch_comfy_unloads_strata(rig):
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy") == 0
    assert not rig.strata.loaded
    assert last(rig, "switch")["previous"] == "strata"


def test_switch_is_idempotent(rig):
    rig.strata.loaded = True
    assert rig.cli("switch", "strata") == 0
    assert "POST /load" not in rig.strata.calls
    assert last(rig, "switch") is None                   # nothing changed, nothing logged


def test_switch_comfy_always_asks_strata(rig):
    """§3.3: Strata may be mid-load with nothing on the GPU yet; /unload waits behind it."""
    assert rig.cli("switch", "comfy") == 0
    assert "POST /unload" in rig.strata.calls


def test_switch_comfy_refused_while_strata_answers(rig, capsys):
    rig.strata.loaded = True
    t = rig.strata.request_async(0.5)
    time.sleep(0.05)
    assert rig.cli("switch", "comfy", "--json") == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "answering or loading" in out["error"]
    t.join()


def test_switch_closed_workload_does_not_block_the_other(rig):
    """A workload that is simply switched off - API dead, nothing on the card, and no
    service vhotseat could start - must not stall or refuse the other one's handover.
    Closing it by hand is a normal way to free the GPU (seen 2026-10-19: a ComfyUI job
    refused because switching to comfy waited 60s on a dead Strata port)."""
    rig.strata.close()
    started = time.monotonic()
    assert rig.cli("switch", "comfy", "--wait", "5") == 0
    assert time.monotonic() - started < 30, "stalled on a port nobody owns (a dead probe costs ~2s here)"
    w = rig.arb.workloads["strata"]
    assert w.busy() is None                       # off the card: idle, not "unknown"
    assert w.release(0, 5) == []                  # nothing to release, nothing to wait for
    # ComfyUI is lazy: with its models not loaded yet the mode stays "none"; the
    # handover is done once nothing else is holding the card.
    assert rig.arb.probe()["mode"] == "none"


def test_switch_closed_but_still_on_the_card_is_refused(rig):
    """Unanswerable is only safe when the GPU counters can see it holding nothing. A
    process that keeps its VRAM after its API dies still blocks, and says what to do."""
    rig.strata.loaded = True
    rig.strata.close()
    assert rig.cli("switch", "comfy", "--wait", "5") == 1
    assert "still holds" in last(rig, "switch")["error"] and "MiB on the GPU" in last(rig, "switch")["error"]


def test_switch_no_service_and_dead_api_fails_fast(rig, capsys):
    """With no service to start it, a dead API is a dead end, not a slow boot: name the
    fix at once instead of waiting out the timeout."""
    from vhotseat import host
    rig.strata.close()
    rig.arb.workloads["strata"].service = host.NoService()
    started = time.monotonic()
    assert rig.cli("switch", "strata", "--json") == 1
    assert time.monotonic() - started < 30, "stalled instead of naming the fix"
    assert "no service configured" in json.loads(capsys.readouterr().out)["error"]


def test_doctor_treats_a_switched_off_workload_as_stopped(rig):
    """doctor: off the card and not answering is a warn with the reason, not a fail."""
    from vhotseat import doctor, host
    rig.strata.close()
    found = [c for c in doctor.checks(rig.arb, rig.cfg) if c[1] == "strata: api"]
    assert found and found[0][0] == "warn" and "treated as stopped" in found[0][2]
    assert not [c for c in doctor.checks(rig.arb, rig.cfg) if c[1] == "strata: vram"]
    rig.strata.loaded = True                               # now it is a stuck process
    found = [c for c in doctor.checks(rig.arb, rig.cfg) if c[1] == "strata: api"]
    assert found and found[0][0] == "fail" and "still holds" in found[0][2]


def test_doctor_spots_attribution_that_stopped_working(rig):
    """When a workload's vram selector stops matching its process, its VRAM lands in
    `other` where nothing can act on it; doctor says so instead of staying silent."""
    from vhotseat import doctor
    rig.gpu.strata_as_other = True
    rig.gpu.other = 4000
    rig.strata.loaded = True
    found = [c for c in doctor.checks(rig.arb, rig.cfg) if c[1] == "strata: vram"
             and "attributes it nothing" in c[2]]
    assert found and found[0][0] == "warn"
    rig.gpu.strata_as_other = False
    assert not [c for c in doctor.checks(rig.arb, rig.cfg) if "attributes it nothing" in c[2]]


def test_doctor_blind_workload_stays_busy(rig):
    """No service and no vram.process: vhotseat can neither ask it nor see it, so an
    unanswerable API must stay busy, and doctor says which blind spot caused it."""
    from vhotseat import doctor, host
    rig.strata.close()
    rig.arb.workloads["strata"].service = host.NoService()
    rig.cfg["workloads"]["strata"].pop("vram", None)
    found = [c for c in doctor.checks(rig.arb, rig.cfg) if c[1] == "strata: vram"]
    assert found and found[0][0] == "warn" and "can block a handover" in found[0][2]
    found = [c for c in doctor.checks(rig.arb, rig.cfg) if c[1] == "strata: api"]
    assert found and found[0][0] == "fail"
    rig.cfg["workloads"]["strata"]["vram"] = {"process": "strata.exe"}   # now it is attributable
    assert not [c for c in doctor.checks(rig.arb, rig.cfg) if c[1] == "strata: vram"]
    assert [c for c in doctor.checks(rig.arb, rig.cfg) if c[1] == "strata: api"][0][0] == "warn"


def test_switch_comfy_waits_for_strata_answer(rig):
    """An answer is never interrupted: --wait lets it finish, then unloads."""
    rig.strata.loaded = True
    t = rig.strata.request_async(0.3)
    time.sleep(0.05)
    assert rig.cli("switch", "comfy", "--wait", "5") == 0
    t.join()
    assert t.result == 200 and not rig.strata.loaded


def test_switch_strata_refused_while_comfy_has_jobs(rig):
    rig.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})
    assert rig.cli("switch", "strata") == 1
    assert "job(s) running or queued" in last(rig, "switch")["error"]
    assert not rig.strata.loaded


def test_switch_starts_stopped_services(rig):
    rig.host.active["strata"] = False
    assert rig.cli("switch", "strata") == 0
    assert rig.host.starts == ["strata"]


def test_stopped_strata_holding_vram_is_reported(rig):
    rig.host.active["strata"] = False
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy") == 1
    assert "strata.service is stopped; kill it by hand" in last(rig, "switch")["error"]


# --- Strata's hook (a request wakes the lazy model) ------------------------------


def test_why_says_what_a_handover_would_have_to_do(rig):
    """`why` is the switch decision read out loud, with nothing switched: what would be
    released, and whether anything stands in the way."""
    rig.strata.loaded = True
    why = rig.arb.why_all()
    assert why["comfy"]["would"] == "ready"
    assert "Strata holds 14000 MiB and would be asked to release it" in why["comfy"]["notes"]
    assert why["strata"]["would"] == "ready"
    assert "it holds the card now" in why["strata"]["notes"]
    assert not [c for c in rig.strata.calls if c.startswith("POST")]   # a dry run only reads


def test_why_waits_behind_running_work(rig):
    rig.comfy.running = ["job1"]
    why = rig.arb.why_all()
    assert why["strata"]["would"] == "waiting"
    assert any(n.startswith("ComfyUI is busy") for n in why["strata"]["notes"])


def test_why_names_the_workload_it_cannot_release(rig):
    """A dead API with VRAM still on the card is what makes a handover fail. `why` says
    so before anything tries, and says what to do about it."""
    rig.strata.loaded = True
    rig.strata.close()
    why = rig.arb.why_all()
    assert why["comfy"]["would"] == "blocked"
    assert any("isn't answering and still holds 14000 MiB" in n for n in why["comfy"]["notes"])


def test_why_shows_the_admission_arithmetic_and_blames_the_right_memory(rig):
    """The admission floor refuses loads, and it refuses them over memory vhotseat can't
    free. Showing the arithmetic is the difference between a mystery and a diagnosis."""
    rig.gpu.other = 15000
    why = rig.arb.why_all()
    assert why["strata"]["would"] == "blocked"
    assert any("only loads with 14000 MiB free" in n for n in why["strata"]["notes"])
    assert any("processes vhotseat doesn't manage" in n for n in why["strata"]["notes"])


def test_a_claim_refuses_a_load_nobody_chose(rig, monkeypatch, capsys):
    """While a game owns the card, a model may not be loaded on top of it. The gate is
    what decides that, so the agent's request comes back as "GPU busy" rather than
    running the game off the machine."""
    monkeypatch.setattr(rig.arb, "claim_hits", lambda: [
        {"pid": 4242, "name": "game.exe", "exe": None, "note": "a Steam game", "by": "game.exe"}])
    assert rig.arb.gate("strata") is False
    assert "claimed by game.exe (a Steam game)" in capsys.readouterr().err
    assert last(rig, "strata-load")["allowed"] is False


def test_a_requested_switch_is_refused_while_a_game_runs(rig, monkeypatch):
    """ComfyUI's guard asks for the card on the queue's behalf: a claim refuses it, so
    a job never loads a 20 GB model over somebody's game."""
    monkeypatch.setattr(rig.arb, "claim_hits", lambda: [
        {"pid": 4242, "name": "game.exe", "exe": None, "note": "a Steam game", "by": "game.exe"}])
    r = rig.arb.switch("comfy", 0, request=True)
    assert r["ok"] is False and "claimed by game.exe" in r["error"]
    assert rig.comfy.vram == COMFY_CONTEXT_VRAM       # nothing loaded, just its CUDA context


def test_a_switch_somebody_typed_is_still_an_order(rig, monkeypatch):
    """The claim rule is about loads nobody chose. `vhotseat switch comfy` at a terminal
    is a decision, and vhotseat doesn't second-guess it."""
    monkeypatch.setattr(rig.arb, "claim_hits", lambda: [
        {"pid": 4242, "name": "game.exe", "exe": None, "note": "a Steam game", "by": "game.exe"}])
    assert rig.arb.switch("comfy", 0)["ok"] is True
    assert rig.comfy.vram > 0


def test_a_requested_switch_that_would_load_nothing_is_not_refused(rig, monkeypatch):
    """A batch mid-flight keeps its next job: the model is already on the card, so the
    next prompt loads nothing, and "never interrupt" outranks a game that started
    between clips. The job that would have *loaded* the model is still refused."""
    game = [{"pid": 4242, "name": "game.exe", "exe": None, "note": "a Steam game", "by": "game.exe"}]
    monkeypatch.setattr(rig.arb, "claim_hits", lambda: game)
    # ComfyUI is lazy: with no model loaded, its first job is a load, so it's refused.
    assert rig.arb.switch("comfy", 0, request=True)["ok"] is False
    rig.arb.switch("comfy", 0)                       # a person's order: card and model
    rig.comfy.vram = 7000                            # model resident, queue empty between clips
    assert rig.arb.switch("comfy", 0, request=True)["ok"] is True


def test_why_blames_the_claim(rig, monkeypatch):
    monkeypatch.setattr(rig.arb, "claim_hits", lambda: [
        {"pid": 4242, "name": "game.exe", "exe": None, "note": "a Steam game", "by": "game.exe"}])
    why = rig.arb.why_all()
    assert why["comfy"]["would"] == "blocked"
    assert any("claimed by game.exe (a Steam game)" in n for n in why["comfy"]["notes"])


def test_status_says_who_owns_the_card_by_right(rig, monkeypatch):
    """The whole path, not a patched answer: a rule, a process table, and the line a
    person reads in `status`. Twenty pids of one game come out as one grouped line."""
    monkeypatch.setattr(rig.arb, "claim_rules",
                        claims.rules([{"name": "game.exe", "note": "a Steam game"}], builtin=False))
    monkeypatch.setattr(host, "processes", lambda: {
        n: {"pid": n, "ppid": 1, "name": "game.exe", "exe": None} for n in (1, 2, 3)})
    s = rig.arb.probe()
    assert len(s["claims"]) == 3
    assert "claims:" in rig.arb.describe(s)
    assert "game.exe x3 (a Steam game)" in rig.arb.describe(s)


def test_cli_claims_lists_what_matches_and_the_rules(rig, monkeypatch, capsys):
    monkeypatch.setattr(rig.arb, "claim_rules",
                        claims.rules([{"name": "game.exe", "note": "the game I play"}], builtin=False))
    monkeypatch.setattr(rig.arb, "claim_hits", lambda: [
        {"pid": 4242, "name": "game.exe", "exe": r"D:\Games\game.exe", "note": "the game I play",
         "by": "game.exe (D:\\Games\\game.exe)"}])
    assert rig.cli("claims", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hits"][0]["name"] == "game.exe"
    assert out["rules"] == [{"name": "game.exe", "note": "the game I play", "on_start": "release"}]
    assert rig.cli("claims") == 0
    text = capsys.readouterr().out
    assert "the card is claimed by game.exe (the game I play)" in text
    assert "- name game.exe  (the game I play)  -> on_start: release" in text   # in force, and what it does


def test_hook_allows_when_comfy_idle(rig):
    assert rig.strata.request() == 200
    e = last(rig, "strata-load")
    assert e["allowed"] and e["source"] == "request" and e["reason"] == "ComfyUI idle"
    assert "available_mib" in e and "comfy" in e


def test_hook_frees_idle_comfy_models(rig):
    rig.comfy.vram = 7000
    assert rig.strata.request() == 200
    assert rig.comfy.vram == 400
    assert last(rig, "strata-load")["reason"] == "ComfyUI idle; freed its 7000 MiB of models"


def test_hook_refuses_while_comfy_renders(rig):
    """A render is never interrupted: Strata answers 503 instead."""
    rig.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})
    time.sleep(0.05)
    assert rig.strata.request() == 503
    e = last(rig, "strata-load")
    assert not e["allowed"] and e["reason"] == "ComfyUI has 1 job(s) running or queued"


def test_hook_refuses_when_comfy_state_unknown(rig):
    """Fail closed: ComfyUI holding models with its API down could be mid-job.
    (gpu-workload used to read an unreachable /queue as 0 jobs.)"""
    rig.comfy.vram = 7000
    rig.comfy.down = True
    assert rig.strata.request() == 503
    assert "whether it has jobs is unknown" in last(rig, "strata-load")["reason"]


def test_hook_allows_when_comfy_down_but_holding_nothing(rig):
    """…but a ComfyUI that's starting up (API not answering yet, nothing on the
    GPU) doesn't keep Strata out."""
    rig.comfy.down = True
    assert rig.strata.request() == 200


def test_hook_refuses_when_nvidia_smi_fails(rig):
    rig.gpu.error = "nvidia-smi failed: boom"
    assert rig.strata.request() == 503
    assert last(rig, "strata-load")["reason"] == "nvidia-smi failed: boom"


def test_hook_refuses_during_switch_comfy(rig):
    with rig.arb.lock.try_take("switch comfy"):
        assert rig.strata.request() == 503
    e = last(rig, "strata-load")
    assert e["source"] == "hook" and e["reason"] == "switch comfy in progress"


# --- races (how-it-works §3) --------------------------------------------------------


def test_race_job_queued_as_hook_frees(rig):
    """§3.1: a job lands between the hook's /queue check and its /free. /free is
    postponed past the job, ComfyUI's VRAM doesn't drop, and the hook refuses
    rather than letting the two share the GPU. ComfyUI is not restarted."""
    rig.comfy.vram = 7000
    rig.comfy.slow_s = 6.0
    rig.comfy.job_arrives_on_free = True
    assert rig.strata.request() == 503
    e = last(rig, "strata-load")
    assert not e["allowed"] and "could not make room in ComfyUI" in e["reason"]
    assert rig.host.restarts == []


def test_race_job_after_hook_allowed(rig):
    """§3.1, second half: a job queued after Strata loaded waits (--wait) for
    Strata's answer, then unloads it, then runs."""
    t = rig.strata.request_async(0.3)
    # The gated load takes ~0.15 s on Windows (its first HTTPS-capable opener, slower file writes).
    deadline = time.monotonic() + 2
    while not rig.strata.loaded and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rig.strata.loaded and t.is_alive()
    assert rig.cli("switch", "comfy", "--wait", "5") == 0
    t.join()
    assert t.result == 200 and not rig.strata.loaded


def test_race_interleaved_jobs_and_request(rig):
    """§3.4: job A done, Strata request, job B: two extra handovers, both safe."""
    rig.comfy.vram = 7000                              # job A finished, models still loaded
    assert rig.strata.request() == 200                 # hook frees ComfyUI, Strata loads
    assert rig.cli("switch", "comfy", "--wait", "5") == 0  # job B's guard
    assert not rig.strata.loaded


# --- deadlocks (how-it-works §4) ------------------------------------------------------


def test_switch_strata_does_not_deadlock_on_its_own_hook(rig):
    """§4.1: switch strata holds the lock and blocks on /load, whose hook wants
    the lock. The hook reads the holder note instead of waiting."""
    done = threading.Event()
    threading.Thread(target=lambda: (rig.cli("switch", "strata"), done.set()), daemon=True).start()
    assert done.wait(5)
    assert rig.strata.loaded


def test_switch_inside_hold_is_a_noop(rig, monkeypatch, capsys):
    """§4.2: a hold's command switching to the held mode."""
    monkeypatch.setenv("VHOTSEAT_HOLD", "comfy")
    assert rig.cli("switch", "comfy", "--json") == 0
    assert json.loads(capsys.readouterr().out)["held"] == "comfy"
    assert rig.cli("switch", "strata") == 1


def test_hold_refuses_strata_until_command_exits(rig, tmp_path):
    rig.strata.loaded = True
    gate = tmp_path / "go"
    seen = {}

    def during():
        while not gate.exists():
            time.sleep(0.01)
        seen["status"] = rig.strata.request()

    t = threading.Thread(target=during, daemon=True)
    t.start()
    code = rig.cli("hold", "comfy", "--", sys.executable, "-c",
                   f"import pathlib,time; pathlib.Path({str(gate)!r}).touch(); time.sleep(0.5)")
    t.join()
    assert code == 0 and seen["status"] == 503
    assert not rig.strata.loaded
    assert [e["event"] for e in rig.events() if e["event"].startswith("hold")] == ["hold", "hold-end"]


def test_hold_takes_options_before_the_command(rig):
    """`hold comfy --wait 600 -- CMD`, as the README writes it: --wait used to be
    taken for the command."""
    assert rig.cli("hold", "comfy", "--wait", "5", "--", sys.executable, "-c", "raise SystemExit(7)") == 7
    assert rig.cli("hold", "--wait", "5", "comfy", "--", sys.executable, "-c", "raise SystemExit(7)") == 7


def test_hold_waits_for_strata_answering(rig):
    """`hold comfy --wait N` while Strata answers: waits for the answer, then holds.
    The command's REMAINDER argument used to swallow `--wait N` after NAME, so the
    hold failed at once with "Strata is answering" (seen 2026-10-19, comfy-render --hold)."""
    rig.strata.loaded = True
    t = rig.strata.request_async(1.0)
    time.sleep(0.1)
    assert rig.cli("hold", "comfy", "--wait", "30", "--", sys.executable, "-c", "pass") == 0
    t.join()
    assert t.result == 200 and not rig.strata.loaded
    assert [e["event"] for e in rig.events() if e["event"].startswith("hold")] == ["hold", "hold-end"]


def test_hold_exit_code_is_the_commands(rig):
    assert rig.cli("hold", "comfy", "--", sys.executable, "-c", "raise SystemExit(7)") == 7


def test_guard_inside_hold_does_not_wait_for_it(rig, tmp_path):
    """The bug in Appendix A.6 #1: a script under `hold comfy` queues a ComfyUI
    job; the job's guard runs `switch comfy` from ComfyUI's process (no
    VHOTSEAT_HOLD in its env). It used to wait for the hold's lock, while
    the script waited for the job: stuck until the guard's 420 s timeout."""
    ready = tmp_path / "ready"
    result = {}

    def guard():
        while not ready.exists():
            time.sleep(0.01)
        t0 = time.monotonic()
        result["code"] = rig.arb.switch("comfy", 300)
        result["s"] = time.monotonic() - t0

    t = threading.Thread(target=guard, daemon=True)
    t.start()
    rig.cli("hold", "comfy", "--", sys.executable, "-c",
            f"import pathlib,time; pathlib.Path({str(ready)!r}).touch(); time.sleep(1)")
    t.join(5)
    assert result["code"] == {"ok": True, "held": "comfy"}
    assert result["s"] < 0.9                           # didn't wait for the hold to end


def test_switch_waits_for_another_switch(rig):
    """Concurrent switches are serialized."""
    with rig.arb.lock.try_take("switch strata"):
        done = threading.Event()
        threading.Thread(target=lambda: (rig.cli("switch", "comfy"), done.set()), daemon=True).start()
        assert not done.wait(0.3)
    assert done.wait(5)


def test_hook_and_guard_wait_is_bounded(rig):
    """§4.3: the hook holds the lock while it frees ComfyUI; a guard starting
    then waits for it, and is never stuck."""
    rig.comfy.vram = 7000
    t = rig.strata.request_async(0.2)
    while "POST /free" not in rig.comfy.calls:        # the hook has the lock
        time.sleep(0.005)
    res = rig.arb.switch("comfy", 10)
    t.join()
    assert res["ok"] and t.result == 200 and not rig.strata.loaded


# --- ComfyUI restart (README "ComfyUI restart: RAM, not just VRAM") ----------------------


def test_restart_when_strata_needs_comfys_ram(rig):
    """Strata's admission isn't met, last job finished 4 s ago: waits ~6 s more
    (so its result can be read from /history), restarts ComfyUI."""
    pid = rig.comfy.queue_job()
    time.sleep(0.3)
    rig.comfy.finish_history(pid, ago=4)
    rig.comfy.vram = 7000
    rig.host.mem["comfyui"] = {"ram_mib": 6000, "swap_mib": 1000, "file_mib": 0}
    rig.host.available_mib = 42000
    rig.host.on_restart["comfyui"] = lambda: (rig.comfy.restarted(), setattr(rig.host, "available_mib", 49000))
    assert rig.strata.request() == 200
    assert rig.host.restarts == ["comfyui"]
    e = last(rig, "strata-load")
    assert e["reason"] == ("ComfyUI idle; freed its 7000 MiB of models; "
                           "restarted ComfyUI to release 7000 MiB of RAM + swap for Strata")


def test_no_restart_when_ram_is_enough(rig):
    rig.comfy.vram = 7000
    rig.host.mem["comfyui"] = {"ram_mib": 9000, "swap_mib": 500, "file_mib": 0}
    assert rig.strata.request() == 200
    assert rig.host.restarts == []


def test_no_restart_if_job_queued_during_settle(rig):
    pid = rig.comfy.queue_job()
    time.sleep(0.3)
    rig.comfy.finish_history(pid, ago=0)
    rig.host.available_mib = 42000
    threading.Timer(0.3, lambda: rig.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}})).start()
    assert rig.strata.request() == 503
    assert rig.host.restarts == []
    assert "queued just now; not restarting it" in last(rig, "strata-load")["reason"]


def test_restart_when_vram_stuck_after_free(rig):
    """An extension that doesn't unload fully (IndexTTS without our patch)."""
    rig.comfy.vram = 7000
    rig.comfy.stuck_vram = 2066
    assert rig.strata.request() == 200
    assert rig.host.restarts == ["comfyui"]
    assert last(rig, "strata-load")["reason"] == "ComfyUI idle; restarted it: kept 2066 MiB of VRAM after /free"


def test_switch_strata_restarts_too(rig):
    rig.comfy.vram = 7000
    rig.host.available_mib = 42000
    rig.host.on_restart["comfyui"] = lambda: (rig.comfy.restarted(), setattr(rig.host, "available_mib", 49000))
    assert rig.cli("switch", "strata") == 0
    assert rig.host.restarts == ["comfyui"] and rig.strata.loaded


# --- the guard node -----------------------------------------------------------------------


def _guard(rig, monkeypatch):
    """The real guard node's take_gpu(), running vhotseat as a subprocess
    (tests/run_cli.py: the real CLI with GPU and services read from the fakes)."""
    sys.path.insert(0, str(ROOT / "integrations" / "comfyui"))
    monkeypatch.setenv("VHOTSEAT_BIN", cli_bin(rig.state.parent))
    monkeypatch.setenv("STRATA_URL", rig.strata.url)
    monkeypatch.setenv("COMFY_URL", rig.comfy.url)
    monkeypatch.setenv("XDG_STATE_HOME", str(rig.state.parent))
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    monkeypatch.setenv("VHOTSEAT_GUARD_WAIT_S", "2")
    sys.modules.pop("vhotseat_guard", None)
    import vhotseat_guard  # its install() logs "ComfyUI internals changed" here: no ComfyUI
    return vhotseat_guard


def test_guard_unloads_strata_before_a_job(rig, monkeypatch):
    guard = _guard(rig, monkeypatch)
    rig.strata.loaded = True
    guard.take_gpu()
    assert not rig.strata.loaded


def test_guard_reports_why_gpu_is_busy(rig, monkeypatch):
    guard = _guard(rig, monkeypatch)
    # The guard waits up to 2 s (VHOTSEAT_GUARD_WAIT_S) for Strata's answer;
    # this one takes longer. (The fake's waits aren't scaled down like the CLI's.)
    rig.strata.loaded = True
    t = rig.strata.request_async(4.0)
    time.sleep(0.05)
    with pytest.raises(guard.GpuBusy, match="Strata is answering or loading"):
        guard.take_gpu()
    t.join()


def test_the_guard_falls_back_when_vhotseat_doesnt_know_request(rig, monkeypatch):
    """An installed vhotseat from before claims rejects --request outright. Every job used to
    fail with argparse's message; it gets the typed switch instead - the weaker one, since a
    claim won't refuse it - and says so once, then stops paying for the failed run."""
    guard = _guard(rig, monkeypatch)
    calls = []

    class Res:
        def __init__(self, code, err):
            self.returncode, self.stderr, self.stdout = code, err, ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "--request" in cmd:
            return Res(2, "usage: vhotseat switch [-h] ...: error: unrecognized arguments: --request")
        return Res(0, "")

    monkeypatch.setattr(guard, "REQUEST_OK", True)
    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    guard.take_gpu()
    assert "--request" in calls[0] and "--request" not in calls[1]
    guard.take_gpu()
    assert len(calls) == 3 and "--request" not in calls[2]
    assert guard.REQUEST_OK is False


def test_switch_with_wait_lets_a_comfy_batch_finish(rig):
    """`switch strata --wait` while ComfyUI has a batch queued, each job running
    the guard (`switch comfy`). It used to hold the lock while waiting for the
    queue, so every guard blocked on it: the batch stalled for the whole wait and
    the switch then failed. Found by the llama-server trial, 2026-10-17."""
    rig.comfy.slow_s = 0.3
    rig.comfy.guard = lambda pid: (lambda r: None if r["ok"] else r["error"])(rig.arb.switch("comfy", 30))
    pids = [rig.comfy.queue_job({"1": {"class_type": "Slow", "inputs": {}}}) for _ in range(4)]
    time.sleep(0.05)
    t0 = time.monotonic()
    res = rig.arb.switch("strata", 20)
    assert res["ok"] and time.monotonic() - t0 < 5
    assert all(rig.comfy.history[p]["status"]["status_str"] == "success" for p in pids)
    assert rig.strata.loaded
