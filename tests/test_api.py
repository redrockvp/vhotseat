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
