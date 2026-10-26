"""Telemetry and `vhotseat web`: samples, events and log lines recorded by the
CLI, read back by the dashboard's API, and pruned after their retention."""

import datetime
import json
import sqlite3
import threading
import time
import urllib.request

import pytest
from fakes import COMFY_CONTEXT_VRAM, COMFY_MODEL_VRAM, STRATA_VRAM

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


def test_an_hour_with_no_samples_in_it_is_not_an_hour_on_the_card():
    """How big a hole counts as a hole was measured from the samples in the window, so a window with
    a handful in it - a box that stopped sampling, a range that starts where the retention does -
    said the history was kept an hour apart, and integrated the hour that wasn't there: the mean of
    the two readings for sixty minutes of a card nobody was looking at. The cadence is in the config
    as `telemetry.interval_s`, so it is what a gap gets judged against."""
    samples = [{"t": 0, "mode": "strata", "gpu_power_w": 300},
               {"t": 3600, "mode": "strata", "gpu_power_w": 300}]
    invented = telemetry.cost(samples, 4.0)                   # no cadence: the samples' own spacing
    assert invented["hours"] == 1.0 and invented["kwh"] == 0.3
    honest = telemetry.cost(samples, 4.0, step_s=5)           # the 5 s the sampler records at
    assert honest == {"kwh": 0.0, "cost": 0.0, "hours": 0.0, "w_now": 300, "by_mode": {}}


def test_the_cost_answers_pass_the_cadence_the_history_is_kept_at(rig, server, monkeypatch):
    """Both callers know what interval recorded the samples they just read, and neither said: the
    dashboard's Power cost card and `vhotseat cost` each priced an outage as if the card had been
    watched through it. Once the gap is a gap, `hours` is what was actually sampled, which is what
    both of them already use to say "this is a floor, not the whole cost"."""
    seen, real = [], telemetry.cost

    def spy(samples, price, step_s=None):
        seen.append(step_s)
        return real(samples, price, step_s)

    rig.gpu.health = lambda: {"power_w": 300.0}
    rig.arb.power = {"price_per_kwh": 4.0}
    rig.cli("status")
    time.sleep(1.1)
    rig.cli("status")
    monkeypatch.setattr(telemetry, "cost", spy)                # `cost` is imported inside the CLI
    monkeypatch.setattr(web, "cost", spy)                      # ... and at the top of web.py
    assert rig.cli("cost", "--since", "1h", "--json") == 0
    code, _ = get(server + "/api/cost?since=0&until=99999999999")
    assert code == 200
    cadence = rig.arb.telemetry.interval_s
    assert seen == [cadence, cadence], "the CLI and the dashboard both said it"


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


def test_a_telemetry_that_cant_be_read_costs_the_page_its_history_not_its_status(rig, server,
                                                                                 monkeypatch):
    """Every status poll asks the history for three numbers: how far back it goes, what the card
    usually carries that nothing here can free, and what each workload has been seen holding. A
    database another process has locked - or one that has gone corrupt - raised through all three,
    /api/status answered 500, and the page went blank: the card in front of the owner, the holders,
    the open claim and the pressure episode all unreadable, because a file the page only draws
    pictures from could not be opened. It falls back the way the pressure readings do, and says which
    of the two - no samples yet, or a file that can't be read - the blank history means."""
    rig.gpu.other = 1200                             # memory nothing on this box can free
    rig.strata.loaded = True

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    for read in ("oldest", "floor_mib", "peaks"):
        monkeypatch.setattr(rig.arb.telemetry, read, locked)
    code, body = get(server + "/api/status")
    st = json.loads(body)
    assert code == 200, st
    assert st["mode"] == "strata"                    # the live answer is still the live answer
    assert st["oldest"] is None and st["interval_s"] == 5
    assert st["floor"] == 1200                       # the card as it reads now, as with no samples
    assert st["peaks"] == {"strata": STRATA_VRAM, "comfy": COMFY_CONTEXT_VRAM}
    assert "database is locked" in st["history_error"]


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


def test_the_dashboard_shows_the_pressure_episode_while_it_runs(rig, server, monkeypatch):
    """Mid-flight: an owner watching models vanish needs the app that took the card, how far past
    its usual floor it sits, and what has come off for it. Those are the numbers the release acted
    on, out of the same probe that read the card, so the explanation can't disagree with the
    decision - and `stuck_tries` rides along because the page counts tries alongside the watcher."""
    from test_pressure_episode import scene
    scene(rig, monkeypatch, grower="blender.exe", grower_mib=9000)
    web.pressure_pass(rig.arb)
    st = json.loads(get(server + "/api/status")[1])
    pr = st["pressure"]
    assert pr["open"]["episode"]["name"] == "blender.exe"
    assert pr["open"]["episode"]["other_mib"] == 9000 and pr["open"]["episode"]["floor_mib"] == 1000
    assert pr["open"]["released"] == ["strata"] and pr["open"]["freed_mib"] == STRATA_VRAM
    assert pr["stuck_tries"] == web.STUCK_TRIES
    assert pr["enabled"] is True and pr["min_free_mib"] == 1500
    assert st["idle"]["strata"] == rig.arb.workloads["strata"].idle_vram_mib


def test_the_dashboard_shows_the_episode_that_already_ended(rig, server, monkeypatch):
    """After the fact: `open` is empty and `last` is the row that closed it, with the reason and
    how much came free. It comes from the event log, so a dashboard opened the next morning still
    says which app was behind the unload, and so does one that never saw the episode happen."""
    from test_pressure_episode import scene
    scene(rig, monkeypatch, grower="blender.exe", grower_mib=9000)
    web.pressure_pass(rig.arb)
    web.pressure_pass(rig.arb)                             # the room came back: it closes
    time.sleep(1.1)                                        # the status cache is a second wide
    pr = json.loads(get(server + "/api/status")[1])["pressure"]
    assert pr["open"] is None
    assert pr["last"]["event"] == "pressure-end" and pr["last"]["name"] == "blender.exe"
    assert pr["last"]["freed_mib"] == STRATA_VRAM


def test_the_spill_into_system_ram_is_recorded_as_evidence(rig):
    """The card's two memory figures mean different things, and a history that keeps only one loses
    the difference. Measured on this box: 52206 MiB of shared GPU memory between 37 processes, 50.9
    GiB of it one workload, while the card was merely full - RAM the card borrowed because it ran
    out, already off it, and never a reason to unload anything. It's kept per sample so a week later
    an owner can see the card was oversubscribed at that moment, which is a different fact with a
    different answer from the card being short."""
    rig.gpu.shared_of = {1: 4000, 2: 9000}
    rig.strata.loaded = True
    st = rig.arb.probe()
    assert st["gpu"]["shared_mib"] == 13000
    sample = rig.arb.telemetry.samples(0, time.time() + 1)[-1]
    assert sample["vram_shared"] == 13000
    rig.gpu.shared_of = {}                    # a backend with no such counter, not an empty card
    assert rig.arb.probe()["gpu"]["shared_mib"] is None


# --- the rules editor: the sentences it is handed, and the questions it asks -----------


def post(url, body, edit="1"):
    """POST the way the page does, with the header and content type the guard asks for. `edit=None`
    is the way a page on somebody else's origin does it."""
    headers = {"Content-Type": "application/json"}
    if edit is not None:
        headers[web.EDIT_HEADER] = edit
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def policy_api(server) -> dict:
    return json.loads(get(server + "/api/policy")[1])


def preview(server, row, col, choice, **extra):
    """Ask the editor's question: what would the (row, col) cell do if the grid said this? The
    draft matrix is the one the page would post - the saved grid with the one cell changed - so
    the answer is about an answer the owner has not committed to yet."""
    cells = {name: {c: v["choice"] for c, v in cols.items()}
             for name, cols in policy_api(server)["matrix"].items()}
    cells[row][col] = choice
    return post(f"{server}/api/policy/preview", {"requester": col, "workload": row,
                                                 "matrix": cells, **extra})


def test_the_policy_api_hands_the_page_the_row_sentence_and_all_three_cell_lines(rig, server):
    """The grid's words are the server's. Each cell comes with the sentence each of its three
    choices carries - so picking Ask me changes the line under it before anything is saved, and
    the page never holds a consequence of its own to fall out of step with the arbiter - and each
    row comes with the sentence it adds up to, which is what says what a whole row does without
    reading five cells."""
    body = policy_api(server)
    assert body["summaries"]["strata"] == ("Strata makes room for anything, including workloads "
                                           "you add later.")
    assert body["says_for"]["strata"]["comfy"] == {
        "yes": "Strata unloads once idle",
        "ask": "ComfyUI waits for your answer",
        "keep": "ComfyUI is refused"}
    assert body["says_for"]["strata"]["pressure"]["keep"] == "nothing is freed"
    assert "strata" not in body["says_for"]["strata"]      # nothing unloads itself
    # The words each column reads in the middle of a sentence: the page's own labels have to use
    # them, or its headings, its aria-labels and its summaries name one thing three ways.
    assert [r["phrase"] for r in body["requesters"]] == ["Strata", "ComfyUI", "a game", "low VRAM"]
    rig.arb.workloads["strata"].asks_for = ("claims",)
    assert policy_api(server)["summaries"]["strata"] == (
        "Strata makes room for ComfyUI and low VRAM and asks you first for a game.")


def test_the_preview_says_what_an_unsaved_cell_would_do_on_this_card(rig, server):
    """Before the owner saves a rule they are told what it does, and the sentence is about the
    card they are looking at: which answer the cell holds, and whether the workload on it is
    answering something now, was used a moment ago so something may be mid-task, or is idle. A
    demo whose state the page could set would be a demo of a situation the owner is not in."""
    rig.strata.loaded = True
    rig.arb.note_used("strata", time.time() - 3600)          # an hour since anything asked
    code, out = preview(server, "strata", "comfy", "keep")
    assert code == 200 and out["tone"] == "warn"
    assert out["title"] == "Strata stays loaded, and you get a warning"
    assert out["detail"] == ("ComfyUI is refused. The warning has a Make room now button, and the "
                             "refusal is logged.")
    code, out = preview(server, "strata", "comfy", "yes")
    assert code == 200 and out["title"] == "Strata unloads right away"
    assert "Strata has been idle for 60 min." in out["detail"]
    assert "Frees 13.7 GiB in about 2 s" in out["detail"]
    code, out = preview(server, "strata", "comfy", "ask")
    assert out["title"] == "You get a notification asking first"
    assert "ComfyUI waits for your answer." in out["detail"]
    assert "No answer in 2 min: Strata stays loaded." in out["detail"]
    # The draft's own ask numbers, not the saved ones: the question the preview describes is the
    # one the owner's unsaved ask block would ask.
    _, out = preview(server, "strata", "comfy", "ask", ask={"timeout_s": 60, "fallback": "release"})
    assert "No answer in 60 s: Strata unloads." in out["detail"]
    # A game and an app that grew cannot be refused, so keeping loaded costs memory rather than a
    # refusal, and the preview says which of the two it is.
    assert preview(server, "strata", "claims", "keep")[1]["detail"] == (
        "A game can't be refused, so it starts with what is free.")
    assert preview(server, "strata", "pressure", "keep",
                   pressure={"enabled": False})[1]["title"] == "Nothing is unloaded"


def test_the_preview_reads_the_workload_s_real_state(rig, server):
    """The same cell answers differently depending on what the loaded workload is doing, and the
    states that matter are the states the release itself obeys: it is working, so its current
    request finishes first; or it is not working but was used a moment ago, so unloading it may
    cut across a task mid-step. Not knowing when it last worked is a third state and is said as
    that, never as idle."""
    rig.strata.loaded = True
    rig.arb.note_used("strata")                             # seconds ago: may well be mid-task
    _, out = preview(server, "strata", "comfy", "yes")
    assert out["tone"] == "warn"
    assert out["title"] == "Strata unloads at the next pause, even mid-task"
    assert "Strata was used 0 s ago, so something may be mid-task." in out["detail"]
    assert "Choose Ask me if you'd rather decide." in out["detail"]
    # A box where nothing has ever stamped this workload has no evidence that it is idle, and
    # calling it idle here is how an agent loses its model between two steps.
    rig.arb.used_path.unlink()
    _, out = preview(server, "strata", "comfy", "yes")
    assert "Nothing has recorded Strata working since this started" in out["detail"]
    # And a workload answering something keeps answering it: the preview says the request in front
    # of it is never the one that gets cut off.
    rig.arb.workloads["strata"].busy = lambda: "Strata is busy"
    _, out = preview(server, "strata", "comfy", "yes")
    assert out["title"] == "Strata finishes its current request, then unloads"
    assert "frees 13.7 GiB for ComfyUI" in out["detail"]


def test_a_preview_changes_no_rule_no_card_and_no_log(rig, server):
    """The preview is a question about the rules, not an edit of them: nothing is written, no cell
    moves, and no row is recorded. It is guarded like the writes all the same, because what it
    hands back is the owner's rules read out loud next to their live card, and a page on another
    origin has no business asking - least of all with a draft grid this page never showed."""
    rig.strata.loaded = True
    before = policy_api(server)
    assert preview(server, "strata", "comfy", "keep")[0] == 200
    after = policy_api(server)
    assert after["matrix"] == before["matrix"] and after["version"] == before["version"]
    assert rig.events() == []
    # Nothing was asked of the card: reading it costs a GET to each API, and the preview that sent
    # one of those is the one that must never send a POST.
    assert not [c for c in rig.strata.calls if c.startswith("POST")]
    assert post(f"{server}/api/policy/preview", {"requester": "comfy"}, edit=None)[0] == 403
    for body in ({"requester": "nosuch"}, {"requester": "comfy", "workload": "nosuch"},
                 {"requester": "strata", "workload": "strata"},
                 {"requester": "comfy", "workload": "strata",
                  "matrix": {"strata": {"comfy": "maybe"}}},
                 {"requester": "comfy", "ask": {"timeout_s": 0}}, "not an object"):
        code, out = post(f"{server}/api/policy/preview", body)
        assert code == 400 and out["error"], body
    assert rig.arb.open_asks() == [] and rig.strata.loaded   # still holding, as it was


def test_nothing_on_the_card_is_answered_as_nothing(rig, server):
    """The card is free, so the rule has nothing to do, and the preview says that instead of
    describing a release that would not happen. Asked with no workload named, which is what the
    page does when its own reading of the card says nobody holds it."""
    assert rig.arb.probe()["mode"] == "none"
    code, out = post(f"{server}/api/policy/preview", {"requester": "comfy"})
    assert code == 200 and out["tone"] == "info"
    assert out["title"] == "Nothing is loaded"
    assert out["detail"] == "Nothing is on the card right now, so this would not unload anything."

