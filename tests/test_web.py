"""Telemetry and `vhotseat web`: samples, events and log lines recorded by the
CLI, read back by the dashboard's API, and pruned after their retention."""

import datetime
import json
import sqlite3
import threading
import time
import urllib.request

import pytest

from vhotseat import config, telemetry, util, web
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
