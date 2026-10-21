"""Claims: what owns the card by right (a game), and what vhotseat refuses while it runs.

The rules are matched against a process table, so they're testable without a game:
`{pid: {pid, ppid, name, exe}}`, the shape host.processes() returns."""

import sys

import pytest

from vhotseat import claims, config, host
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def procs(*rows):
    """(pid, ppid, name, exe) -> a process table."""
    return {pid: {"pid": pid, "ppid": ppid, "name": name, "exe": exe} for pid, ppid, name, exe in rows}


def rules(*raw):
    return claims.rules(raw, builtin=False)


def test_an_idle_workload_is_not_claimed_by_its_own_directory(rig, monkeypatch):
    r"""A workload installed under D:\Games\ matches the built-in rule for a game in a Games
    folder. The exclusion came from the holder list - the top few by memory - so an idle
    workload wasn't in it, and it blocked its own loads by existing."""
    procs = {7: {"pid": 7, "ppid": 4, "name": "strata.exe", "exe": r"D:\Games\strata\strata.exe"},
             8: {"pid": 8, "ppid": 4, "name": "EldenRing.exe", "exe": r"D:\Games\elden\EldenRing.exe"},
             9: {"pid": 9, "ppid": 4, "name": "dwm.exe", "exe": r"C:\Windows\System32\dwm.exe"}}
    monkeypatch.setattr(host, "processes", lambda: procs)
    monkeypatch.setattr(rig.arb, "claim_rules",
                        claims.rules([{"path": r"*\Games\*", "note": "a game in a Games folder"}], builtin=False))
    rig.arb.workloads["strata"].vram_selector = ("process", "games\\strata\\")
    hits = rig.arb._claims_in([])                  # nobody on the card, so the holder list is empty
    assert [h["name"] for h in hits] == ["EldenRing.exe"]


def test_a_game_directory_claims_the_card():
    rs = rules({"path": r"*\steamapps\common\*", "note": "a Steam game"})
    p = procs((10, 4, "eldenring.exe", r"C:\Steam\steamapps\common\ELDEN RING\eldenring.exe"))
    hits = claims.matching(rs, p)
    assert [h["name"] for h in hits] == ["eldenring.exe"]
    assert hits[0]["note"] == "a Steam game"
    assert "steamapps" in hits[0]["by"]


def test_a_known_game_claims_the_card_without_a_readable_path():
    """An elevated or another user's process has no readable image path, so the exe
    name is all there is: a name rule still works, and a path rule doesn't crash."""
    rs = rules({"name": "EldenRing.exe", "note": "Elden Ring"}, {"path": r"*\Games\*"})
    p = procs((11, 4, "EldenRing.exe", None))
    hits = claims.matching(rs, p)
    assert [h["note"] for h in hits] == ["Elden Ring"]


def test_a_parent_rule_claims_what_a_launcher_started():
    rs = rules({"parent": "mylauncher.exe", "note": "launched by MyLauncher"})
    p = procs((1, 0, "services.exe", None), (2, 1, "mylauncher.exe", r"C:\MyLauncher\mylauncher.exe"),
              (3, 2, "game.exe", r"D:\stuff\game.exe"), (4, 3, "eac_launcher.exe", None))
    hits = claims.matching(rs, p)
    assert {h["name"] for h in hits} == {"game.exe", "eac_launcher.exe"}   # two levels deep
    assert "launched by mylauncher.exe" in hits[0]["by"]


def test_launchers_never_claim_the_card_however_they_are_installed():
    """Steam, Epic and the rest run all day, and their helpers live inside the same
    directories the game rules point at. If a launcher could claim the card, nothing
    would ever be allowed to load on this machine."""
    builtin = claims.rules()
    p = procs((1, 0, "steam.exe", r"C:\Program Files (x86)\Steam\bin\steam.exe"),
              (2, 1, "steamwebhelper.exe", r"C:\Program Files (x86)\Steam\bin\cef\steamwebhelper.exe"),
              (3, 0, "EpicGamesLauncher.exe", r"C:\Program Files\Epic Games\Launcher\EpicGamesLauncher.exe"),
              (4, 0, "RiotClientServices.exe", r"C:\Riot Games\Riot Client\RiotClientServices.exe"))
    assert claims.matching(builtin, p) == []


def test_the_builtin_rules_recognise_the_common_installations():
    builtin = claims.rules() if sys.platform == "win32" else claims.rules()
    p = procs((1, 0, "game.exe", r"C:\Program Files (x86)\Steam\steamapps\common\Some Game\game.exe"),
              (2, 0, "RobloxPlayerBeta.exe", r"C:\Users\me\AppData\Local\Roblox\RobloxPlayerBeta.exe"),
              (3, 0, "anything", "/home/me/Games/porting/something"),
              (4, 0, "python3", "/usr/bin/python3"))
    hits = claims.matching(builtin, p)
    names = {h["name"] for h in hits}
    assert "game.exe" in names and "RobloxPlayerBeta.exe" in names
    assert "python3" not in names


def test_a_workload_pid_never_claims_its_own_card():
    rs = rules({"path": "*Games*"})
    p = procs((7, 4, "comfyui.exe", r"C:\Games\ComfyUI\python.exe"))
    assert claims.matching(rs, p) != []
    assert claims.matching(rs, p, exclude={7}) == []


def test_one_process_one_explanation():
    """Two rules matching the same process is one claim, described by the first."""
    rs = rules({"name": "game.exe", "note": "by name"}, {"path": "*Games*", "note": "by path"})
    hits = claims.matching(rs, procs((1, 0, "game.exe", r"D:\Games\game.exe")))
    assert len(hits) == 1


def test_describe_groups_twenty_processes_into_one():
    """A browser, and a game with its anti-cheat helpers, is twenty pids. Twenty
    clauses in a refusal explains nothing; one grouped line does."""
    hits = [{"pid": n, "name": "firefox.exe", "note": "a stand-in", "by": "firefox.exe"} for n in range(20)]
    hits.append({"pid": 99, "name": "game.exe", "note": "a Steam game", "by": "game.exe"})
    assert claims.describe(hits) == "firefox.exe x20 (a stand-in), game.exe (a Steam game)"


def test_a_broken_parent_chain_does_not_loop():
    """A snapshot taken while processes die can point a parent at a pid already
    reused, or at itself."""
    p = procs((1, 2, "a.exe", None), (2, 1, "b.exe", None))
    assert len(claims.ancestors(1, p)) <= 12
    assert claims.ancestors(3, p) == []


def test_claim_rules_are_validated():
    """A typo in a rule would silently claim the wrong thing - or nothing."""
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    raw["claims"] = [{"name": "somegame.exe", "note": "the game I play"}]
    raw["claims_builtin"] = False
    cfg = config.validate(raw)
    assert cfg["claims"][0]["name"] == "somegame.exe" and cfg["claims_builtin"] is False
    assert config.validate({**raw, "claims": [], "claims_builtin": True})["claims_builtin"] is True
    # what isn't in the config isn't in the dict either: the default lives in the reader
    assert config.validate({k: v for k, v in raw.items() if k != "claims_builtin"}).get(
        "claims_builtin", True) is True
    raw["claims"] = [{"nmae": "somegame.exe"}]
    with pytest.raises(config.ConfigError, match=r"claims\[0\]: unknown key"):
        config.validate(raw)
    raw["claims"] = [{"note": "no matcher at all"}]
    with pytest.raises(config.ConfigError, match=r"needs at least one of name, path, parent"):
        config.validate(raw)
    raw["claims"] = "somegame.exe"
    with pytest.raises(config.ConfigError, match=r"claims: expected a list"):
        config.validate(raw)
    raw["claims"] = []
    raw["claims_builtin"] = "yes"
    with pytest.raises(config.ConfigError, match=r"claims_builtin"):
        config.validate(raw)


def test_a_game_process_can_be_pointed_at_by_name_or_path_in_the_config():
    """The example config's commented rules must be valid YAML once uncommented: a
    commented-out rule that wouldn't parse is a trap for whoever uncomments it."""
    raw = config.parse((ROOT / "config.example.yaml").read_text())
    raw["claims"] = [{"path": r"*\World of Warcraft\*", "note": "World of Warcraft"},
                     {"name": "rocketleague.exe"}]
    cfg = config.validate(raw)
    rs = claims.rules(cfg["claims"], builtin=cfg.get("claims_builtin", True))
    hits = claims.matching(rs, procs((1, 0, "Wow.exe", r"C:\World of Warcraft\Wow.exe"),
                                     (2, 0, "RocketLeague.exe", None)))
    assert {h["note"] for h in hits} == {"World of Warcraft", "claimed"}
