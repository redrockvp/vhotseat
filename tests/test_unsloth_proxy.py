"""The Unsloth Studio gate: a proxy that takes the card before a request loads weights.

The rules that matter here are vhotseat's: never interrupt what's running, refuse rather
than load on top, and count a request as in flight until its last byte has reached the
client. The upstream is a fake Studio that loads, streams and unloads on request.
"""

import http.client
import importlib.util
import json
import pathlib
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest

from fakes import _Server
from test_vhotseat import vh          # the same fake box, for one test against a real arbiter
from vhotseat import api

PROXY_FILE = pathlib.Path(__file__).resolve().parents[1] / "integrations" / "unsloth" \
    / "vhotseat_unsloth_proxy.py"


def load_proxy():
    spec = importlib.util.spec_from_file_location("vhotseat_unsloth_proxy", PROXY_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


proxy = load_proxy()


class FakeStudio(_Server):
    """Loads by model_path, streams an answer slowly, unloads on request."""

    def __init__(self):
        self.loaded: list[str] = []
        self.load_s = 0.0
        self.stream_s = 0.0
        self.hold_s = 0.0          # an upgraded connection kept open this long
        self.stream_started = threading.Event()
        self.unloaded: list[str] = []
        super().__init__()

    def route(self, h, method, url, body):
        path, q = url
        self.calls.append(f"{method} {path}")
        if path == "/api/inference/loaded-models":
            return h.send_json(200, {"data": [{"id": m} for m in self.loaded]})
        if path == "/echo":
            return h.send_json(200, {"path": path, "query": q, "auth": self.last_headers.get("authorization")})
        if path == "/ws":
            h.send_response(101)
            h.send_header("Upgrade", "websocket")
            h.send_header("Connection", "Upgrade")
            h.end_headers()
            for _ in range(int(self.hold_s * 5)):
                h.wfile.write(b"\x81\x00")        # an empty websocket frame
                h.wfile.flush()
                time.sleep(0.2)
            h.close_connection = True
            return
        if path == "/api/inference/load" and method == "POST":
            time.sleep(self.load_s)
            self.loaded.append((body or {}).get("model_path"))
            return h.send_json(200, {"ok": True})
        if path == "/v1/chat/completions" and method == "POST":
            self.stream_started.set()
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.end_headers()
            for _ in range(3):
                h.wfile.write(b"data: a token\n\n")
                h.wfile.flush()
                time.sleep(self.stream_s)
            h.close_connection = True       # a stream ends where the server ends it
            return
        if path == "/api/inference/unload" and method == "POST":
            self.unloaded.append((body or {}).get("model_path"))
            self.loaded.clear()
            return h.send_json(200, {"ok": True})
        h.send_json(404, {"error": path})


class StubSeat:
    """A seat that only records: what the proxy asks, and whether it says no."""

    def __init__(self, owner=None, refuse=None):
        self.owner, self.refuse, self.acquires = owner, refuse, []

    def owns(self, name):
        return self.owner == name

    def acquire(self, name, wait=0):
        self.acquires.append((name, wait))
        if self.refuse:
            raise api.Busy(self.refuse)
        self.owner = name


def call(base, method, path, body=None):
    up = urlsplit(base)
    conn = http.client.HTTPConnection(up.hostname, up.port, timeout=10)
    conn.request(method, path, json.dumps(body) if body is not None else None,
                 {"Content-Type": "application/json", "Authorization": "Bearer sekrit"})
    resp = conn.getresponse()
    raw = resp.read().decode()
    out = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw)
    conn.close()
    return out


@pytest.fixture
def studio():
    srv = FakeStudio()
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture
def gate(studio):
    seat = StubSeat(owner="comfy")
    p = proxy.Proxy(seat, studio.url, workload="unsloth", hold=5.0).start()
    try:
        yield p, seat
    finally:
        p.stop()


def test_a_completion_request_is_a_load_request():
    """The whole reason the proxy exists: a chat request naming a model Studio hasn't loaded
    puts weights on the card, so it must hold the card first."""
    yes = ["/api/inference/load", "/v1/load", "/v1/chat/completions", "/v1/completions",
           "/api/inference/images/load", "/api/inference/audio/stt/load"]
    no = ["/api/inference/unload", "/api/inference/load-progress", "/api/inference/loaded-models",
          "/api/inference/estimate-memory", "/api/settings/embedding-model/unload"]
    for path in yes:
        assert proxy.loads_a_model("POST", path), path
    for path in no:
        assert not proxy.loads_a_model("POST", path), path
    assert not proxy.loads_a_model("GET", "/v1/chat/completions")


def test_reading_studio_is_passed_through_untouched(gate, studio):
    p, seat = gate
    code, _, raw = call(p.url, "GET", "/echo?repo_id=unsloth/Qwen3.8-27B-GGUF")
    got = json.loads(raw)
    assert code == 200 and got["query"] == {"repo_id": "unsloth/Qwen3.8-27B-GGUF"}
    assert got["auth"] == "Bearer sekrit"       # the key passes through; the proxy has none
    assert seat.acquires == []                  # and no handover for a read


def test_a_load_takes_the_card_before_it_is_forwarded(gate, studio):
    p, seat = gate
    code, _, _ = call(p.url, "POST", "/api/inference/load", {"model_path": "repo:Q4_K_XL"})
    assert code == 200 and seat.owner == "unsloth" and studio.loaded == ["repo:Q4_K_XL"]
    assert seat.acquires == [("unsloth", 5.0)]  # asked, with the hold time it was given
    code, _, _ = call(p.url, "POST", "/v1/chat/completions", {"model": "repo:Q4_K_XL"})
    assert code == 200
    assert len(seat.acquires) == 1              # it owns the card now: the fast path


def test_a_refused_handover_is_a_503_with_a_retry_after(studio):
    seat = StubSeat(owner="comfy", refuse="ComfyUI is rendering")
    p = proxy.Proxy(seat, studio.url, workload="unsloth", hold=1.0).start()
    try:
        code, headers, raw = call(p.url, "POST", "/api/inference/load", {"model_path": "repo:Q4"})
        assert code == 503 and headers["retry-after"] == "30"
        assert "GPU busy" in json.loads(raw)["error"]
        assert studio.calls == []               # it never reached the server: nothing loaded
    finally:
        p.stop()


def test_a_request_is_in_flight_until_its_last_byte(studio):
    studio.stream_s = 0.3
    seat = StubSeat(owner="comfy")
    p = proxy.Proxy(seat, studio.url, workload="unsloth", hold=5.0).start()
    try:
        thread = threading.Thread(target=lambda: call(p.url, "POST", "/v1/chat/completions", {"model": "m"}))
        thread.start()
        studio.stream_started.wait(5)
        assert p.status()["in_flight"] == 1     # mid-stream: a handover must wait for it
        thread.join(5)
        assert p.status()["in_flight"] == 0
    finally:
        p.stop()


def test_a_request_waiting_on_the_card_is_already_in_flight(studio):
    """A drain must see a request that has not forwarded yet. Counting only from the moment of
    forwarding let a drain unload the server and hand the card on while a load was still
    waiting for the card - which then loaded on top of whoever had just been given it."""
    class SlowSeat(StubSeat):
        def acquire(self, name, wait=0):
            self.started.set()
            time.sleep(0.5)
            super().acquire(name, wait)

    seat = SlowSeat(owner="comfy")
    seat.started = threading.Event()
    p = proxy.Proxy(seat, studio.url, workload="unsloth", hold=5.0).start()
    try:
        threading.Thread(target=lambda: call(p.url, "POST", "/api/inference/load",
                                             {"model_path": "m"})).start()
        seat.started.wait(5)
        assert p.status()["in_flight"] == 1        # counted before it was forwarded
        code, _, raw = call(p.url, "POST", "/_vhotseat/drain?wait=0.1")
        assert code == 409, (code, raw)            # refuse, don't unload underneath it
    finally:
        p.stop()


def test_a_request_arriving_while_a_drain_waits_does_not_break_the_drain(studio):
    """A request must not stay counted while it waits for a drain to end: the drain waits for
    in_flight to reach zero. With one request still streaming, a second one that arrived during
    the drain held the count at one forever, the drain timed out into a 409 (vhotseat's release
    fails with Busy) and the request then got its own 503. This is a0c8182's fix for the other
    race, taken one step too far."""
    studio.stream_s = 0.4                       # ~1.2 s of tokens still reaching the client
    seat = StubSeat(owner="unsloth")
    p = proxy.Proxy(seat, studio.url, workload="unsloth", hold=5.0, unload=lambda: None).start()
    out = {}

    def go(key, method_path, body):
        out[key] = call(p.url, "POST", method_path, body)[0]

    threading.Thread(target=go, args=("first", "/v1/chat/completions", {"model_path": "m"})).start()
    studio.stream_started.wait(5)               # weights on the card, streaming
    t = threading.Thread(target=go, args=("drain", "/_vhotseat/drain?wait=3", None))
    t.start()
    time.sleep(0.2)                             # the drain is waiting on the stream
    r = threading.Thread(target=go, args=("second", "/api/inference/load", {"model_path": "m2"}))
    r.start()
    time.sleep(0.3)
    assert p.status()["draining"] is True       # and the second request lands mid-drain
    t.join(15)
    r.join(15)
    assert out["drain"] == 200, out             # the drain cleared when the stream ended
    assert out["second"] == 200, out            # and the request that arrived went through


def test_an_open_websocket_does_not_hold_the_card_hostage(studio):
    """A UI tab keeps its websocket open for as long as it is open. Counting that as in flight
    means every drain answers 409 while somebody has a window on the screen, and the card can
    never be taken back."""
    studio.hold_s = 1.2
    seat = StubSeat(owner="unsloth")
    p = proxy.Proxy(seat, studio.url, workload="unsloth", hold=5.0).start()
    up = urlsplit(p.url)
    sock = socket.create_connection((up.hostname, up.port), timeout=5)
    sock.sendall(b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
    try:
        assert b"101" in sock.recv(200)            # spliced through to the server
        time.sleep(0.3)
        assert p.status()["in_flight"] == 0        # the open tab is not "work running"
        code, _, raw = call(p.url, "POST", "/_vhotseat/drain?wait=1")
        assert code == 200, (code, raw)
    finally:
        sock.close()
        p.stop()


def test_draining_stops_forwarding_then_unloads(studio):
    unloaded = []
    seat = StubSeat(owner="unsloth")
    p = proxy.Proxy(seat, studio.url, workload="unsloth", hold=5.0,
                    unload=lambda: unloaded.append("asked")).start()
    studio.loaded = ["repo:Q4"]
    try:
        code, _, raw = call(p.url, "POST", "/_vhotseat/drain?wait=5")
        assert code == 200 and json.loads(raw)["drained"] is True
        assert unloaded == ["asked"] and p.status()["draining"] is False
    finally:
        p.stop()


def test_a_drain_that_find_requests_running_says_busy_and_clears(studio):
    studio.stream_s = 0.6
    seat = StubSeat(owner="unsloth")
    p = proxy.Proxy(seat, studio.url, workload="unsloth", hold=5.0).start()
    try:
        threading.Thread(target=lambda: call(p.url, "POST", "/v1/chat/completions", {"model": "m"})).start()
        studio.stream_started.wait(5)
        code, _, raw = call(p.url, "POST", "/_vhotseat/drain?wait=0.1")
        assert code == 409 and json.loads(raw)["in_flight"] == 1
        assert p.status()["draining"] is False  # a drain that never clears is a stuck proxy
    finally:
        p.stop()


def test_ten_requests_at_once_cause_one_handover(vh):
    """Once one gated request has taken the card, a stampede behind it changes hands again
    no times: the seat knows who it just handed the card to, so the rest take the fast path
    while the first request's weights are still landing."""
    assert vh.arb.switch("comfy", 0)["ok"] is True
    seat = api.Seat(vh.arb)
    p = proxy.Proxy(seat, vh.strata.url, workload="strata", hold=10.0).start()
    try:
        assert call(p.url, "POST", "/v1/chat/completions", {"model": "m"})[0] == 200
        results, threads = [], []
        for _ in range(10):
            t = threading.Thread(target=lambda: results.append(
                call(p.url, "POST", "/v1/chat/completions", {"model": "m"})))
            t.start()
            threads.append(t)
        for t in threads:
            t.join(15)
        bad = [r for r in results if r[0] != 200]
        assert len(results) == 10 and not bad, bad[:2]
        events = [(e.get("to"), e.get("ok")) for e in vh.events() if e.get("event") == "switch"]
        handovers = [e for e in events if e[0] == "strata" and e[1]]
        assert len(handovers) == 1, events
        assert vh.arb.probe()["mode"] == "strata"
        comfy = vh.arb.workloads["comfy"]
        assert vh.strata.loaded and vh.comfy.vram <= comfy.idle_vram_mib   # off the card, context aside
    finally:
        p.stop()
