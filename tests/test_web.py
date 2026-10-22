"""Telemetry and `vhotseat web`: samples, events and log lines recorded by the
CLI, read back by the dashboard's API, and pruned after their retention."""

import datetime
import json
import sqlite3
import threading
import time
import urllib.request

import pytest

from fakes import COMFY_MODEL_VRAM, STRATA_VRAM
from vhotseat import claims, config, host, telemetry, util, web
from vhotseat.telemetry import Telemetry


def test_cli_commands_record_samples_events_and_logs(rig):
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy") == 0
    tel = rig.arb.telemetry
    assert (rig.state / "telemetry.db").exists()
    samples = tel.samples(0, time.time() + 1)
    assert samples and samples[-1]["mode"] in ("comfy", "none")
    assert set(samples[-1]["w"]) == {"strata", "comfy"}
    assert {"vram", "ram", "swap"} <= set(samples[-1]["w"]["comfy"])
    events = tel.events(0, time.time() + 1)
    assert events[0]["event"] == "switch" and events[0]["to"] == "comfy"
    # The event log keeps its own copy: the jsonl file is unchanged.
    assert rig.events()[-1]["to"] == "comfy"


def test_log_lines_are_kept_with_their_command(rig, capsys):
    util.log("not recorded: no command is running")
    assert rig.cli("switch", "nosuch") == 2
    rig.cli("status")
    util.log("not recorded either")
    tel = rig.arb.telemetry
    tel.source = "switch strata"
    util.LOG_SINK = tel.log
    try:
        util.log("hello from a switch")
    finally:
        util.LOG_SINK = None
    logs = tel.logs(0, time.time() + 1)
    assert [x["message"] for x in logs] == ["hello from a switch"]
    assert logs[0]["source"] == "switch strata"
    assert "hello from a switch" in capsys.readouterr().err


def test_retention_prunes_old_rows(tmp_path):
    tel = Telemetry(tmp_path / "t.db", retention_hours=1)
    tel.event({"event": "old"})
    db = sqlite3.connect(tmp_path / "t.db")
    db.execute("UPDATE events SET t = t - 7200")
    db.commit()
    tel._last_prune = 0
    tel.event({"event": "new"})
    assert [e["event"] for e in tel.events(0, time.time() + 1)] == ["new"]


def test_telemetry_never_breaks_a_command(tmp_path):
    # A path that can't be a database: every write is swallowed.
    (tmp_path / "dir").mkdir()
    tel = Telemetry(tmp_path / "dir", retention_hours=1)
    tel.event({"event": "x"})
    tel.log("x")


def test_samplers_in_several_processes_do_not_double_up(tmp_path, rig):
    a = Telemetry(tmp_path / "t.db", interval_s=5)
    b = Telemetry(tmp_path / "t.db", interval_s=5)
    assert a.due() and b.due()
    a.sample(rig.arb.probe())
    assert not a.due() and not b.due()


def test_telemetry_config(rig):
    raw = json.loads(json.dumps(rig.cfg))
    raw["telemetry"] = {"retention_hours": 48, "interval_s": 10}
    config.validate(raw)
    for bad in ({"retention_hours": 0}, {"interval_s": "5"}, {"keep": 3}, {"enabled": "no"}):
        raw["telemetry"] = bad
        with pytest.raises(config.ConfigError, match="telemetry"):
            config.validate(raw)


def test_bucket_averages_into_points():
    samples = [{"t": float(i), "mode": "comfy" if i < 50 else "strata", "vram_free": i,
                "w": {"comfy": {"vram": 100, "ram": None, "swap": 0}}} for i in range(100)]
    out = web.bucket(samples, 10)
    assert len(out) == 10
    assert out[0]["vram_free"] == round(sum(range(10)) / 10)
    assert out[0]["n"] == 10 and out[-1]["mode"] == "strata"
    assert out[0]["w"]["comfy"] == {"vram": 100, "ram": None, "swap": 0}
    assert web.bucket(samples[:5], 10) == samples[:5]


@pytest.fixture
def server(rig):
    dash = web.Dashboard(rig.arb)
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_web_serves_the_page_and_history_recorded_before_it_started(rig, server):
    # Recorded by CLI commands before the dashboard was opened.
    rig.strata.loaded = True
    rig.cli("switch", "comfy")
    rig.cli("switch", "strata")
    code, page = get(server + "/")
    assert code == 200 and b"<title>vhotseat</title>" in page
    code, body = get(server + "/api/status")
    st = json.loads(body)
    assert code == 200 and st["workloads"] == ["strata", "comfy"] and st["retention_hours"] == 24
    since, until = time.time() - 60, time.time() + 1
    hist = json.loads(get(f"{server}/api/history?since={since}&until={until}")[1])
    assert hist and all("vram_total" in s for s in hist)
    events = json.loads(get(f"{server}/api/events?since={since}&until={until}")[1])
    assert [e["to"] for e in events if e["event"] == "switch"][:2] == ["strata", "comfy"]
    assert get(f"{server}/api/history?since=x")[0] == 400
    assert get(f"{server}/api/nope")[0] == 404
    code, icon = get(server + "/favicon.svg")
    assert code == 200 and icon.startswith(b"<svg") and b'href="/favicon.svg"' in page


def test_the_dashboard_is_the_claim_recorder_wherever_it_runs(rig, server, monkeypatch):
    """A box that runs `vhotseat web` already has something probing the machine every few
    seconds. It should record the claims itself rather than needing a second process."""
    monkeypatch.setattr(rig.arb, "claim_rules",
                        claims.rules([{"name": "game.exe", "note": "a Steam game"}], builtin=False))
    monkeypatch.setattr(host, "processes", lambda: {1: {"pid": 1, "ppid": 1, "name": "game.exe", "exe": None}})
    get(f"{server}/api/status")
    monkeypatch.setattr(host, "processes", lambda: {})
    time.sleep(1.1)                       # the status cache's second
    get(f"{server}/api/status")
    rows = [e for e in rig.events() if e["event"] in ("claim", "claim-end")]
    assert [(e["event"], e["name"], e["source"]) for e in rows] == [
        ("claim", "game.exe", "watch"), ("claim-end", "game.exe", "watch")]


def test_watch_records_one_claim_row_per_kind_of_process(rig, monkeypatch):
    """One game is twenty processes, and a browser or an anti-cheat helper is more. The
    log should say the card was claimed - once - and when it came back."""
    polls = {"n": 0}

    def hits():
        polls["n"] += 1
        if polls["n"] > 2:
            return []                                  # the game closed
        return [{"pid": n, "name": "game.exe", "exe": None, "note": "a Steam game",
                 "by": "game.exe"} for n in (11, 12)]

    monkeypatch.setattr(rig.arb, "claim_hits", hits)
    monkeypatch.setattr(rig.arb.telemetry, "interval_s", 0.02)
    assert web.watch(rig.arb, polls=3) == 0
    rows = [e for e in rig.events() if e["event"] in ("claim", "claim-end")]
    assert [(e["event"], e["name"], e.get("processes")) for e in rows] == [
        ("claim", "game.exe", 2), ("claim-end", "game.exe", 2)]


def test_claim_events_are_per_claim_not_per_process(rig):
    """One game is twenty processes. When one of them exits the other nineteen still own the
    card, so the log must not say the card is open again. And the state is read from the log,
    not from a process's memory: `web` and `watch` recording together write the row once, and
    `watch` restarting doesn't re-open a claim that is still running."""
    hits = [{"pid": 10, "name": "eldenring.exe", "exe": None, "note": "a Steam game", "by": "path x"},
            {"pid": 11, "name": "eldenring.exe", "exe": None, "note": "a Steam game", "by": "path x"}]
    web.claim_transitions(rig.arb, hits)
    web.claim_transitions(rig.arb, hits)        # a second recorder, same claim
    web.claim_transitions(rig.arb, hits[:1])    # one of the game's processes exited
    rows = [e for e in rig.arb.events.tail() if e["event"] in ("claim", "claim-end")]
    assert [e["event"] for e in rows] == ["claim"] and rows[0]["processes"] == 2
    web.claim_transitions(rig.arb, [])          # the game is gone
    rows = [e for e in rig.arb.events.tail() if e["event"] in ("claim", "claim-end")]
    assert [e["event"] for e in rows] == ["claim", "claim-end"]


def test_sampler_samples_in_the_background(rig, monkeypatch):
    monkeypatch.setattr(rig.arb.telemetry, "interval_s", 1)
    with telemetry.Sampler(rig.arb):
        time.sleep(2.5)
    assert len(rig.arb.telemetry.samples(0, time.time() + 1)) >= 2


def test_a_new_database_starts_with_the_event_log(tmp_path):
    now = time.time()
    old = {"time": "2020-01-01T00:00:00+00:00", "event": "switch", "to": "comfy"}
    # As EventLog writes it (an offset with a colon: Python 3.10 parses no other).
    when = datetime.datetime.fromtimestamp(now - 60).astimezone().isoformat(timespec="seconds")
    recent = {"time": when, "event": "hold", "mode": "comfy"}
    (tmp_path / "events.jsonl").write_text("\n".join([json.dumps(old), "not json", json.dumps(recent)]) + "\n")
    tel = Telemetry(tmp_path / "t.db", seed_events=tmp_path / "events.jsonl")
    assert [e["event"] for e in tel.events(0, now + 1)] == ["hold"]
    # Only once: an existing database isn't seeded again.
    tel2 = Telemetry(tmp_path / "t.db", seed_events=tmp_path / "events.jsonl")
    assert len(tel2.events(0, now + 1)) == 1


def test_gpu_load_temperature_and_power_are_recorded_and_served(rig, server):
    health = {"name": "NVIDIA GeForce RTX 5070 Ti", "util_pct": 97, "temp_c": 71, "power_w": 285.5, "power_limit_w": 300.0}
    rig.gpu.health = lambda: dict(health)
    rig.cli("status")
    s = rig.arb.telemetry.samples(0, time.time() + 1)[-1]
    assert (s["gpu_util_pct"], s["gpu_temp_c"], s["gpu_power_w"], s["gpu_power_limit_w"]) == (97, 71, 285.5, 300.0)
    st = json.loads(get(server + "/api/status")[1])
    assert st["gpu_health"]["name"] == "NVIDIA GeForce RTX 5070 Ti"
    # `status --json` itself is unchanged: health is the dashboard's.
    assert "gpu_health" not in rig.arb.probe()


def test_web_serves_the_handover_dry_run(rig, server):
    """The page's answer to "why can't this job have the card?" comes from the same dry
    run the CLI prints, not a guess made in the page."""
    rig.strata.loaded = True
    code, body = get(f"{server}/api/why")
    assert code == 200
    why = json.loads(body)["targets"]
    assert why["comfy"]["would"] == "ready"
    assert "Strata holds 14000 MiB and would be asked to release it" in why["comfy"]["notes"]


def test_cost_integrates_the_cards_wattage_per_holder():
    """kWh and cost over the sampled gaps, charged to whoever held the card at the gap's
    start. A stretch with no samples - or no wattage - is no data, not a card that drew
    nothing, so it is skipped rather than counted as free."""
    samples = [{"t": 0, "mode": "strata", "gpu_power_w": 200},
               {"t": 60, "mode": "strata", "gpu_power_w": 200},
               {"t": 120, "mode": "strata", "gpu_power_w": 200},
               {"t": 180, "mode": "comfy"},                              # the driver didn't say
               {"t": 1260, "mode": "comfy", "gpu_power_w": 400},         # a 18-minute hole
               {"t": 1320, "mode": "comfy", "gpu_power_w": 400}]
    out = telemetry.cost(samples, 4.0)
    assert out["kwh"] == 0.013 and out["cost"] == 0.05 and out["w_now"] == 400
    assert out["by_mode"]["strata"] == {"kwh": 0.007, "cost": 0.03, "hours": 0.03}
    assert out["by_mode"]["comfy"] == {"kwh": 0.007, "cost": 0.03, "hours": 0.02}


def test_cost_reports_what_the_price_buys(rig, capsys):
    """Without a price it says what's missing; with one, it prices the history per holder."""
    rig.gpu.health = lambda: {"power_w": 300.0}
    rig.arb.power = {}
    assert rig.cli("cost", "--since", "1h") == 2
    assert "price_per_kwh" in capsys.readouterr().err
    rig.arb.power = {"price_per_kwh": 4.0, "currency": "R"}
    rig.strata.loaded = True                                              # charge the watts to Strata
    rig.cli("status")
    time.sleep(1.1)                                                       # one sample a second
    rig.cli("status")
    capsys.readouterr()
    assert rig.cli("cost", "--since", "1h", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["price"] == 4.0 and out["w_now"] == 300.0 and "strata" in out["by_mode"]
    assert rig.cli("cost", "--since", "yesterday") == 2


def test_web_serves_the_power_cost(rig, server):
    """The page's Power cost card reads /api/cost; no price is a 404 the page turns into
    a note, not a broken panel."""
    rig.gpu.health = lambda: {"power_w": 250.0}
    rig.arb.power = {}
    rig.cli("status")
    time.sleep(1.1)
    rig.cli("status")
    code, body = get(server + "/api/cost")
    assert code == 404 and "price_per_kwh" in json.loads(body)["error"]
    rig.arb.power = {"price_per_kwh": 4.0, "currency": "R", "note": "home tariff"}
    code, body = get(server + "/api/cost?since=0&until=99999999999")
    out = json.loads(body)
    assert code == 200 and out["currency"] == "R" and out["note"] == "home tariff"
    assert out["w_now"] == 250.0 and out["by_mode"]


def test_a_backend_without_health_still_records(rig):
    rig.cli("status")
    s = rig.arb.telemetry.samples(0, time.time() + 1)[-1]
    assert "gpu_util_pct" not in s and s["vram_total"] == 16303


def test_the_dashboard_shows_the_policy_and_warns_about_a_refusal(rig, server):
    """Two things the page has to say without being told: what the policy in the config is,
    and that it just refused something. The refusal is read back from the event log, which is
    what lets a `switch --request` run by another process warn on this page too."""
    rig.arb.workloads["strata"].yields_to = ("claims",)
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy", "--request") == 1
    st = json.loads(get(server + "/api/status")[1])
    assert st["policy"]["strata"] == {"priority": 50, "yields_to": ["claims"],
                                      "gives_way_for": "a claim on the card"}
    warnings = st["policy_warnings"]
    assert [w["event"] for w in warnings] == ["policy-refused"]
    assert warnings[0]["requested"] == "comfy" and warnings[0]["workloads"] == ["strata"]
    assert "Strata holds 14000 MiB" in warnings[0]["why"]


def test_a_policy_warning_stops_being_news_after_a_few_minutes(rig, server):
    """A banner that never goes away is wallpaper, and this one says somebody may be short of
    VRAM *now*. The row's own age decides, so it is the same answer in every process that
    serves a page."""
    rig.arb.workloads["strata"].yields_to = ("claims",)
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy", "--request") == 1
    fetched = json.loads(get(server + "/api/status")[1])["policy_warnings"]
    assert [w["event"] for w in fetched] == ["policy-refused"]
    log = rig.state / "events.jsonl"
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    old = (datetime.datetime.now().astimezone() - datetime.timedelta(minutes=9)).isoformat(timespec="seconds")
    for r in rows:
        if r["event"] == "policy-refused":
            r["time"] = old
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    time.sleep(1.1)                       # the status cache's second
    assert json.loads(get(server + "/api/status")[1])["policy_warnings"] == []


def test_a_kept_for_policy_decision_reaches_the_banner_too(rig, server, monkeypatch):
    """A claim that wasn't allowed to unload something leaves the game short of VRAM. The row
    is written by the watcher - here by `watch`, which is what runs at logon - and the page
    that shows it may be a different process that never saw the claim."""
    rig.arb.claim_rules = claims.rules([{"name": "game.exe", "note": "a game"}], builtin=False)
    monkeypatch.setattr(host, "processes", lambda: {7: {"pid": 7, "ppid": 4, "name": "game.exe", "exe": None}})
    rig.arb.workloads["strata"].yields_to = ("comfy",)
    rig.strata.loaded = True
    web.watch(rig.arb, polls=1)
    st = json.loads(get(server + "/api/status")[1])
    assert [w["event"] for w in st["policy_warnings"]] == ["policy-kept"]
    assert st["policy_warnings"][0]["workload"] == "strata"


def test_the_dashboard_shows_which_two_workloads_fit_on_the_card_together(rig, server):
    """"Can I run both?" answered from what the card was seen doing, not from what it happens
    to be doing: each workload at the most it has been recorded holding, plus the memory
    nothing here can free. The fakes' card is 16303 MiB, so 14000 + 7000 doesn't fit."""
    rig.cli("status")                                # nothing loaded yet: no peaks to go on
    time.sleep(1.1)                                 # the status cache's second
    out = json.loads(get(server + "/api/status")[1])
    assert out["fits"] == [{"a": "strata", "b": "comfy", "mib": None, "fits": None,
                            "why": "never recorded holding models: strata, comfy"}]
    rig.strata.loaded = True
    rig.cli("status")
    rig.strata.loaded = False
    rig.comfy.vram = COMFY_MODEL_VRAM
    rig.cli("status")
    time.sleep(1.1)
    out = json.loads(get(server + "/api/status")[1])
    assert out["peaks"] == {"strata": STRATA_VRAM, "comfy": COMFY_MODEL_VRAM}
    assert out["floor"] == 0                        # the fakes' card has nothing else on it
    assert out["fits"] == [{"a": "strata", "b": "comfy", "mib": STRATA_VRAM + COMFY_MODEL_VRAM,
                            "fits": False, "why": None}]


def test_an_evening_of_gaming_doesnt_make_every_pair_read_no_room(tmp_path):
    """The floor is what the card *usually* carries, not what it carried at its worst. A game,
    Blender, or whatever a claim rule watches all count as non-workload memory while they run,
    and all of it comes back when they exit. Read that window at its peak and every pair of
    workloads says "no room" for the rest of the retention window after one evening with a game
    open; the median says what the desktop costs on an ordinary day. The same number is what the
    pressure trigger will use for how much can never be freed, which is why it lives in
    Telemetry rather than next to the tiles."""
    tel = telemetry.Telemetry(tmp_path / "t.db", retention_hours=24)
    others = [300, 320, 280, 340, 8000, 8100, 7900, 310, 290]      # an evening with a game
    for other in others:
        tel._write("INSERT INTO samples VALUES (?, ?, ?)",
                   (time.time() - 60, "none", json.dumps({"vram_other": other,
                                                          "w": {"strata": {"vram": 4000},
                                                                "comfy": {"vram": 7000}}})))
    peaks, idle = tel.peaks(["strata", "comfy"]), {"strata": 1500, "comfy": 1500}
    assert peaks == {"strata": 4000, "comfy": 7000}     # the workloads are still read at their worst
    assert tel.floor_mib() == 320                        # the median, not the 8100 peak
    assert web.fits_on_card(peaks, tel.floor_mib(), idle, 16303)[0]["fits"] is True
    # ... and the number this corrects: the same window read at its worst says no room.
    assert web.fits_on_card(peaks, max(others), idle, 16303)[0]["fits"] is False


def test_a_floor_from_an_empty_window_is_none_not_a_zero(tmp_path):
    """No samples is not the same as a card with nothing else on it: the dashboard falls back to
    what the card reads right now, and a 0 out of an empty table would quietly promise room."""
    assert telemetry.Telemetry(tmp_path / "empty.db").floor_mib() is None


def test_peaks_are_read_from_the_samples_and_pruned_with_them(tmp_path):
    """The window is the retention window: a peak from outside it is gone, which is what keeps
    the fit honest about what this machine has been doing lately rather than about whatever it
    did once. Same for the floor."""
    tel = telemetry.Telemetry(tmp_path / "t.db", retention_hours=1)
    tel._write("INSERT INTO samples VALUES (?, ?, ?)",
               (time.time() - 60, "strata", json.dumps({"vram_other": 900,
                                                        "w": {"strata": {"vram": 14000}}})))
    assert tel.peaks(["strata", "comfy"]) == {"strata": 14000, "comfy": None}
    assert tel.floor_mib() == 900
    tel._db.execute("UPDATE samples SET t = t - 7200")
    assert tel.peaks(["strata"]) == {"strata": None}
    assert tel.floor_mib() is None


def test_the_dashboard_survives_an_open_claim(rig, server, monkeypatch):
    """The record of an open claim is keyed by (process name, what claimed it), which is what
    `status` and `claims` unpack - and a JSON object can't have a tuple for a key. So with a
    claim running, `/api/status` died inside json.dumps and the page got a closed connection:
    precisely when it has most to show, that the card is somebody else's and here is what was
    unloaded for it."""
    rig.arb.claim_rules = claims.rules([{"name": "game.exe", "note": "a Steam game"}], builtin=False)
    monkeypatch.setattr(host, "processes",
                        lambda: {5: {"pid": 5, "ppid": 4, "name": "game.exe", "exe": None}})
    rig.strata.loaded = True
    web.watch(rig.arb, polls=1)                       # the claim, and the release it asked for
    assert not rig.strata.loaded
    code, body = get(f"{server}/api/status")
    assert code == 200, body
    st = json.loads(body)
    assert [c["name"] for c in st["claims_state"]] == ["game.exe"]
    assert st["claims_state"][0]["released"] == ["strata"]
    assert st["claims_state"][0]["freed_mib"]


def test_the_fits_matrix_counts_every_pair_and_the_floor_that_comes_with_them():
    """Three workloads make three pairs, and each pair carries the memory nothing on this
    side can free. The one honest answer when a workload has never been sampled holding
    models is unknown: a pair including it has never been seen, and saying it would have
    fitted would be a promise the history didn't earn."""
    peaks = {"strata": 14000, "comfy": 7000, "unsloth": None}
    idle = {"strata": 1500, "comfy": 1500, "unsloth": 1500}
    assert web.fits_on_card(peaks, 900, idle, 24576) == [
        {"a": "strata", "b": "comfy", "mib": 21900, "fits": True, "why": None},
        {"a": "strata", "b": "unsloth", "mib": None, "fits": None,
         "why": "never recorded holding models: unsloth"},
        {"a": "comfy", "b": "unsloth", "mib": None, "fits": None,
         "why": "never recorded holding models: unsloth"},
    ]
    assert web.fits_on_card(peaks, 900, idle, 16303)[0]["fits"] is False
    assert web.fits_on_card(peaks, 900, idle, None) == []      # card unreadable: no comparison
