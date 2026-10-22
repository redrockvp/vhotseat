"""Pressure: an app vhotseat doesn't manage taking the card.

A game names itself and phase 1 frees the card for it. A Blender render or a CUDA script doesn't:
no rule covers it and nobody asked vhotseat for anything, so a release is the only way to answer.
These tests are the decision half - what counts as growth, what doesn't, and how much to give up.
The acting half is in test_pressure_episode.py.
"""

from pathlib import Path

import pytest

from vhotseat import config, pressure

ROOT = Path(__file__).resolve().parents[1]


def built(block=None):
    """config.example.yaml, with a `pressure:` block written into it (or left commented out, which
    is how the file itself has it)."""
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    if block is not None:
        raw["pressure"] = block
    return config.validate(raw)


# --- the config block ------------------------------------------------------------


def test_the_trigger_is_on_by_default_and_slow_to_anger():
    """The defaults are the safety, since this is a feature that acts without being asked. They come
    off a measurement of this box: a 24576 MiB card sitting at 779-958 MiB free with a 21394 MiB
    model loaded, and dwm, firefox, explorer, Discord and Orca holding 3096-3269 MiB between them,
    varying by 173 MiB in three minutes. min_free sits under that ordinary bottom so "low" means a
    change, and grow_mib is ten times that jitter - because a wrong firing costs unloading and
    reloading ~20 GB, possibly a server restart."""
    cfg = pressure.cfg(built().get("pressure"))
    assert cfg == pressure.cfg(None) == dict(pressure.DEFAULTS)
    assert cfg["enabled"] is True and not cfg["ignore"]
    assert cfg["min_free_mib"] == 512 and cfg["grow_mib"] == 2000
    assert cfg["sustain_s"] == 30 and cfg["cooldown_s"] == 600
    assert cfg["headroom_mib"] == 1000
    assert cfg["end_margin_mib"] > cfg["headroom_mib"], "the end margin is the wider one"


def test_the_release_aims_past_the_line_that_was_tripped():
    """A release that only just clears `min_free_mib` leaves the app that grew short again on the
    next allocation, and the episode restarts as soon as it ends. The need is the line plus the
    headroom, and never negative."""
    assert pressure.need_mib(free=300, min_free=512, headroom=1000) == 1212
    assert pressure.need_mib(free=1600, min_free=512, headroom=1000) == 0


def test_a_pressure_block_of_nonsense_is_rejected():
    for block, why in [({"nope": 1}, "unknown key"),
                       ({"enabled": "yes"}, "pressure.enabled"),
                       ({"min_free_mib": -5}, "min_free_mib"),
                       ({"grow_mib": "big"}, "grow_mib"),
                       ({"sustain_s": "30"}, "sustain_s"),
                       ({"cooldown_s": -1}, "cooldown_s"),
                       ({"ignore": "vmware-vmx.exe"}, "pressure.ignore"),
                       ({"ignore": [""]}, "pressure.ignore[0]")]:
        with pytest.raises(config.ConfigError) as got:
            built(block)
        assert why in str(got.value), f"{block} said: {got.value}"


def test_the_owner_can_write_the_block_and_it_survives():
    cfg = pressure.cfg(built({"enabled": False, "ignore": ["vmware-vmx.exe", "*cudaball*"],
                              "min_free_mib": 2000, "cooldown_s": 0}).get("pressure"))
    assert cfg["enabled"] is False and cfg["min_free_mib"] == 2000 and cfg["cooldown_s"] == 0
    assert cfg["ignore"] == ["vmware-vmx.exe", "*cudaball*"]


# --- what counts as growth -------------------------------------------------------

def _row(pid, name, mib, workload=None, exe=None):
    return {"pid": pid, "name": name, "mib": mib, "workload": workload, "exe": exe}


HOLDERS = [_row(1, "strata.exe", 20096, "strata"),
           _row(2, "dwm.exe", 1370),
           _row(3, "explorer.exe", 362),
           _row(4, "blender.exe", 9000, exe=r"C:\Program Files\Blender Foundation\blender.exe"),
           _row(5, "discrandummy.exe", 211)]


def test_growth_is_the_unmanaged_memory_over_what_it_usually_carries():
    """The card being full is not pressure; the memory vhotseat can't manage being far above its
    usual floor is. The floor is what the telemetry recorded as typical (3 GiB here), not the card's
    total and not its worst moment."""
    unmanaged = sum(r["mib"] for r in pressure.growers(HOLDERS))
    assert unmanaged == 1370 + 362 + 9000 + 211
    assert pressure.grow_mib(HOLDERS, floor=3103) == 10943 - 3103
    assert pressure.grow_mib(HOLDERS, floor=None) == 10943     # no history: measure against zero


def test_the_grower_named_in_the_record_is_the_biggest_thing_nothing_manages():
    assert [g["name"] for g in pressure.growers(HOLDERS)] == ["blender.exe", "dwm.exe",
                                                              "explorer.exe", "discrandummy.exe"]


def test_a_workload_filling_the_card_is_not_pressure():
    """Strata loading a bigger model is the gate's business: vhotseat can ask it for the memory
    back, and it already decides those loads. It is never also the app the card went short for."""
    only_strata = [_row(1, "strata.exe", 22000, "strata"), _row(2, "dwm.exe", 1370)]
    assert pressure.growers(only_strata) == [_row(2, "dwm.exe", 1370)]
    assert pressure.grow_mib(only_strata, floor=3103) == 0


def test_a_process_a_claim_already_covers_is_not_also_a_grower():
    """A game is phase 1's: its rule says what to do with it, and it was already given the card.
    Counting it again would write a second episode over the same memory."""
    game = _row(9, "eldenring.exe", 6000)
    rows = [*HOLDERS, game]
    names = [g["name"] for g in pressure.growers(rows, claimed={9})]
    assert "eldenring.exe" not in names
    assert pressure.grow_mib(rows, floor=3103, claimed={9}) == pressure.grow_mib(HOLDERS, 3103)


def test_a_process_the_owner_excluded_is_never_the_reason():
    """A VM with a passthrough, a benchmark and a miner are supposed to have the card. `ignore` is
    the owner saying so, and then nothing of ours comes back for it: the memory goes out of the
    measurement, not just out of the reported name."""
    vm = [_row(7, "vmware-vmx.exe", 16000, exe=r"C:\VMs\vmware-vmx.exe")]
    rows = [_row(1, "strata.exe", 20096, "strata"), _row(2, "dwm.exe", 1370), *vm]
    assert pressure.grow_mib(rows, floor=3103) == 14267               # the VM is the card's now
    assert pressure.grow_mib(rows, floor=3103, ignore=pressure.ignores(["vmware-vmx.exe"])) == 0
    assert pressure.grow_mib(rows, floor=3103, ignore=pressure.ignores([r"*vms*"])) == 0


# --- what to give up -------------------------------------------------------------


def test_the_release_set_stops_as_soon_as_the_need_is_paid_for():
    """Two models unloaded where one paid for it is a reload the owner never asked for. The order
    the set is taken in is the config's `priority:`: lowest first, because that is which workload
    the owner would rather lose."""
    cand = [("comfy", 7000), ("strata", 20000)]
    assert pressure.targets(cand, need=5000) == ["comfy"]
    assert pressure.targets(cand, need=21000) == ["comfy", "strata"]
    assert pressure.targets(cand, need=0) == []


def test_the_owner_s_order_is_not_reordered_to_save_a_release():
    """Two cheap workloads come before one expensive one because the config says so, even though
    the expensive one alone would have covered the need. `priority:` answers which workload the
    owner would rather lose; the trigger doesn't get to reopen that."""
    cand = [("comfy", 1000), ("unsloth", 1000), ("strata", 20000)]
    assert pressure.targets(cand, need=1500) == ["comfy", "unsloth"]
    assert pressure.targets(cand, need=2500) == ["comfy", "unsloth", "strata"]


def test_a_workload_holding_nothing_is_not_in_a_release_set():
    """The app that grew is short of VRAM, not of the RAM a model with no card weights costs:
    unloading it would be a restart for the app's benefit and nothing else."""
    assert pressure.targets([("comfy", 0), ("strata", 9000)], need=4000) == ["strata"]


def test_the_record_says_who_grew_and_what_the_card_was_doing():
    ep = {"episode": {"name": "blender.exe", "other_mib": 10943, "grew_mib": 7840,
                      "floor_mib": 3103, "free_mib": 240, "low_for_s": 34, "shared_mib": 12000}}
    line = pressure.describe(ep)
    assert "blender.exe holds 10943 MiB vhotseat doesn't manage, 7840 MiB over its usual 3103" in line
    assert "card at 240 MiB free for 34 s" in line and "12000 MiB shared" in line
    assert "pressure" not in line and "51.8" not in line       # no invented precision
    assert pressure.describe({}) == ""


def test_a_browser_with_30_gib_of_shared_gpu_memory_is_not_pressure():
    """The counters report two figures per process, and the difference is the whole point: shared is
    system RAM the card borrowed once it ran out, which is already off the card and comes back on
    its own. Measured on this box: 52206 MiB of it between 37 processes, 50.9 GiB of that one
    workload, on a card that was merely full. Reading the two as one number would mean a dozen new
    browser tabs were reason to unload a 21 GiB model."""
    rows = [{"pid": 1, "mib": 300, "name": "firefox.exe", "workload": None, "shared_mib": 30720},
            {"pid": 2, "mib": 1370, "name": "dwm.exe", "workload": None, "shared_mib": 0}]
    assert pressure.grow_mib(rows, floor=1000) == 1670 - 1000        # dedicated only
    for r in rows:                                                   # ten times the spill
        r["shared_mib"] = (r.get("shared_mib") or 0) * 10
    assert pressure.grow_mib(rows, floor=1000) == 1670 - 1000
    assert [r["name"] for r in pressure.growers(rows)] == ["dwm.exe", "firefox.exe"]
