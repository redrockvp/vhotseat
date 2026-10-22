"""vhotseat.api: what a long-running gate (a proxy) builds on. The fake gate
here forwards "requests" for a workload the way docs/guides/proxy-gate.md
describes: fast path while it owns the GPU, acquire() otherwise."""

import threading
import time

import pytest

from vhotseat import api
from vhotseat.errors import Busy


@pytest.fixture
def seat(rig):
    return api.Seat(rig.arb)


class FakeProxy:
    """A gate for ComfyUI in a long-lived process: before each request, the fast
    path (owns, no lock) or the slow path (acquire)."""

    def __init__(self, seat, name):
        self.seat, self.name = seat, name
        self.slow = 0

    def request(self, wait=5):
        if not self.seat.owns(self.name):
            self.slow += 1
            self.seat.acquire(self.name, wait)


def test_a_gate_asking_through_the_seat_is_a_request_so_claims_refuse_it(rig, seat, monkeypatch):
    """Everything that reaches a proxy is a load nobody typed, so it asks: while the card is
    claimed, it is refused. A person typing `vhotseat switch comfy` has decided, and still
    gets the card - which is exactly the difference the guard's --request flag carries."""
    hits = [{"pid": 4242, "name": "EldenRing.exe", "exe": "", "note": "Elden Ring", "by": "name"}]
    monkeypatch.setattr(rig.arb, "claim_hits", lambda: hits)
    with pytest.raises(api.SwitchError):
        seat.acquire("comfy", 0)
    assert rig.cli("switch", "comfy") == 0            # the typed order is not refused


def test_freeing_the_card_is_never_refused_by_a_claim(rig, seat, monkeypatch):
    """A claim stops loads nobody chose. Work getting *off* the card isn't one: with a game
    running, release_all() (switch to none) has to unload, and the release that phase 1 runs on
    a claim start goes through this same door. Regression from e52b123, which made every Seat
    switch a request."""
    hits = [{"pid": 4242, "name": "EldenRing.exe", "exe": "", "note": "Elden Ring", "by": "name"}]
    monkeypatch.setattr(rig.arb, "claim_hits", lambda: hits)
    rig.strata.loaded = True
    assert rig.cli("switch", "strata") == 0          # the typed order puts it on the card
    seat.release_all(wait=5)                         # must not raise
    assert not rig.strata.loaded
    assert rig.arb.probe()["mode"] == "none"
    with pytest.raises(api.SwitchError):
        seat.acquire("strata", 0)                    # loading under the same claim still refused


def test_a_seat_asking_for_the_card_is_refused_by_a_policy_too(rig, seat):
    """Everything a proxy forwards is a load nobody typed, so a workload that won't give way
    for the newcomer refuses the acquire() the same way a claim refuses it, and raises the
    refusal rather than unloading anything. The owner typing `vhotseat switch comfy` has
    decided, and still gets the card."""
    rig.arb.workloads["strata"].yields_to = ("claims",)
    rig.strata.loaded = True
    with pytest.raises(api.SwitchError, match="yields_to"):
        seat.acquire("comfy", 0)
    assert rig.strata.loaded                                   # 14 GB stayed put
    assert rig.cli("switch", "comfy") == 0                     # the typed order is not refused
    assert not rig.strata.loaded


def test_a_seat_that_just_took_the_card_owns_it_while_the_weights_land(rig, seat):
    """A server that loads on request shows nothing on the card until its weights arrive, so
    a probe straight after acquire() says nobody holds the GPU. The seat knows better: it
    just handed the card over, and until someone else takes it, this gate owns it."""
    rig.cli("switch", "comfy")
    seat.acquire("strata", wait=5)
    assert not rig.strata.loaded            # nothing has loaded: this is that window
    assert seat.owns("strata") is True
    rig.cli("switch", "comfy")             # hands change: the memory goes
    assert seat.owns("strata") is False


def test_generation_bumps_on_every_handover(rig, seat):
    g0 = seat.generation()
    rig.cli("switch", "strata")
    g1 = seat.generation()
    rig.cli("switch", "comfy")
    assert g1 > g0 and seat.generation() > g1


def test_gate_bumps_the_generation(rig, seat):
    g0 = seat.generation()
    assert rig.strata.request() == 200
    assert seat.generation() > g0


def test_fast_path_skips_the_lock(rig, seat, monkeypatch):
    proxy = FakeProxy(seat, "comfy")
    rig.comfy.vram = 7000          # ComfyUI holds the GPU
    proxy.request()
    probes = []
    monkeypatch.setattr(rig.arb, "probe", lambda: probes.append(1) or {"mode": "comfy"})
    for _ in range(50):
        proxy.request()
    assert proxy.slow == 0 and probes == []      # 50 requests, no probe, no lock


def test_cache_dropped_when_someone_else_takes_the_gpu(rig, seat):
    proxy = FakeProxy(seat, "comfy")
    rig.comfy.vram = 7000
    proxy.request()
    assert rig.strata.request() == 200           # Strata's gate takes it: generation bumps
    proxy.request()
    assert proxy.slow == 1 and not rig.strata.loaded


def test_owns_is_false_while_another_switch_runs(rig, seat):
    rig.comfy.vram = 7000
    assert seat.owns("comfy")
    with rig.arb.lock.try_take("switch strata"):
        assert seat.holder() == "switch strata"
        assert not seat.owns("comfy")
    assert seat.holder() == ""


def test_acquire_raises_busy_and_never_interrupts(rig, seat):
    rig.strata.loaded = True
    t = rig.strata.request_async(1.0)
    time.sleep(0.05)
    with pytest.raises(Busy, match="answering"):
        seat.acquire("comfy", wait=0)
    t.join()
    assert t.result == 200


def test_acquire_does_not_load(rig, seat):
    """A proxy forwards the request, which loads the model itself."""
    seat.acquire("strata", wait=1)
    assert not rig.strata.loaded


def test_release_all(rig, seat):
    rig.strata.loaded = True
    seat.release_all(wait=1)
    assert not rig.strata.loaded and seat.owner() == "none"


def test_many_threads_one_handover(rig, seat):
    """A burst of requests from a proxy's threads: one handover, not one each."""
    rig.strata.loaded = True
    proxy = FakeProxy(seat, "comfy")
    threads = [threading.Thread(target=proxy.request) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not rig.strata.loaded
    assert [e["to"] for e in rig.events() if e["event"] == "switch"] == ["comfy"]


def test_drain_before_release(rig, tmp_path):
    """release.pre: a proxy stops forwarding and finishes its requests before the
    server is told to unload."""
    w = rig.arb.workloads["strata"]
    calls = []
    orig = w._call

    def spy(spec, timeout=5.0, **fmt):
        calls.append(spec.get("post") or spec.get("get"))
        if "drain" in calls[-1]:
            return 200, {"in_flight": 0}
        return orig(spec, timeout, **fmt)

    w._call = spy
    w.spec["release"]["pre"] = {"post": "http://127.0.0.1:9/_vhotseat/drain?wait={wait}"}
    rig.strata.loaded = True
    assert rig.cli("switch", "comfy") == 0
    assert calls.index("http://127.0.0.1:9/_vhotseat/drain?wait={wait}") < calls.index("/unload?wait={wait}")
