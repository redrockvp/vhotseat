"""`vram: {process: [...]}`: a workload found by any of several substrings.

On the owner's box the 8080 slot is served sometimes by Strata (strata.exe, elevated, so only
its name can be read) and sometimes by a llama-server started by hand from Unsloth's build
(~\\.unsloth\\llama.cpp\\...\\llama-server.exe --port 8080). One substring can't match both, and
'unsloth' (the Unsloth Studio workload's match) catches the second one. A list does: whichever
substring matches first, in the order the workloads are listed, wins.
"""

import pytest

from vhotseat import claims, config, gpu, host

STUDIO = r"C:\Users\owner\.unsloth\studio\unsloth_studio\Scripts\python.exe"
LLAMA = r"C:\Users\owner\.unsloth\llama.cpp\build\bin\Release\llama-server.exe"


def raw_with(process) -> dict:
    return {"workloads": {"strata": {
        "driver": "http", "url": "http://127.0.0.1:8080", "vram": {"process": process},
        "resident": {"get": "/health", "path": "loaded"}, "release": {"post": "/unload"},
        "gate": {"kind": "hook"}}}}


def test_a_list_of_substrings_is_a_valid_selector():
    cfg = config.validate(raw_with(["strata", "--port 8080"]))
    assert cfg["workloads"]["strata"]["vram"]["process"] == ["strata", "--port 8080"]


@pytest.mark.parametrize("bad", [[], [""], ["strata", 8080], [None]])
def test_a_list_selector_needs_non_empty_strings(bad):
    with pytest.raises(config.ConfigError, match=r"workloads\.strata\.vram"):
        config.validate(raw_with(bad))


def test_a_single_string_still_works():
    assert config.validate(raw_with("strata"))["workloads"]["strata"]["vram"]["process"] == "strata"


def test_the_built_workload_carries_every_substring():
    from vhotseat.drivers.base import Context
    w = config.workloads(config.validate(raw_with(["strata", "--port 8080"])), Context(gpu=lambda: {}))[0]
    assert w.vram_selector[0] == "process"
    assert list(w.vram_selector[1]) == ["strata", "--port 8080"]


def test_either_substring_claims_the_process_before_unsloth_does():
    cmds = {
        1: ("", ""),                                                  # strata.exe: elevated, name only
        2: ("", f"{LLAMA} --model x.gguf --host 127.0.0.1 --port 8080"),
        3: ("", f"{STUDIO} -X utf8 -c ... studio --api-only -H 127.0.0.1 -p 8888"),
    }
    procs = [(1, 14000, "strata.exe"), (2, 21985, "llama-server.exe"), (3, 188, "python.exe")]
    sel = {"strata": ("process", ("strata", "--port 8080")),
           "comfy": ("process", r"anaconda3\envs\comfy\python.exe"),
           "unsloth": ("process", "unsloth")}
    rows = {r["pid"]: r["workload"] for r in gpu.holders(24576, 0, procs, sel, procinfo=lambda p: cmds[p])}
    assert rows == {1: "strata", 2: "strata", 3: "unsloth"}
    snap = gpu.attribute(24576, 0, procs, sel, procinfo=lambda p: cmds[p])
    assert (snap["strata_mib"], snap["unsloth_mib"], snap["other_mib"]) == (35985, 188, 0)


def test_a_process_a_list_selector_owns_is_never_a_claim(rig, monkeypatch):
    """The claim scan credits a workload's own processes to it (a server under D:\\Games\\ must
    not claim the card against itself); with a list, any of the substrings does that."""
    rig.arb.workloads["strata"].vram_selector = ("process", ("strata", "llama.cpp"))
    monkeypatch.setattr(rig.arb, "claim_rules",
                        claims.rules([{"name": "llama-server.exe", "note": "x"}], builtin=False))
    monkeypatch.setattr(rig.arb, "claim_ignore", claims.ignores())
    procs = {77: {"pid": 77, "ppid": 4, "name": "llama-server.exe", "exe": LLAMA}}
    monkeypatch.setattr(host, "processes", lambda: procs)
    assert rig.arb.claim_hits() == []
