import argparse
import ast
import re
import shlex
from pathlib import Path

import pytest

from vhotseat import cli
from vhotseat.kit import comfy_render

SKILL = Path(__file__).resolve().parents[1] / "kit/skills/vhotseat-gpu/SKILL.md"


def check_commands(markdown):
    parser = cli.build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for block in re.findall(r"```[^\n]*\n(.*?)```", markdown, re.S):
        for line in block.splitlines():
            words = shlex.split(line, posix=False)
            if not words or words[0] not in ("vhotseat", "comfy-render"):
                continue
            if words[0] == "vhotseat":
                assert len(words) > 1 and words[1] in subparsers.choices, line
                command_parser = subparsers.choices[words[1]]
                words = words[2:]
            else:
                command_parser = comfy_render._parser()
                words = words[1:]
            for word in words:
                if word == "--":
                    break
                if word.startswith("--"):
                    assert word.split("=", 1)[0] in command_parser._option_string_actions, line


def test_commands_match_parsers():
    check_commands(SKILL.read_text(encoding="utf-8"))


def test_checker_rejects_wrong_flag():
    with pytest.raises(AssertionError):
        check_commands("```sh\nvhotseat hold comfy --invented 1 -- command\n```")


def test_exit_codes_match_constants():
    text = SKILL.read_text(encoding="utf-8")
    listed = {name: int(value) for name, value in re.findall(r"\| `(EXIT_\w+)` \| (\d+) \|", text)}
    actual = {name: getattr(comfy_render, name) for name in vars(comfy_render) if name.startswith("EXIT_")}
    assert listed == actual


def test_json_contract_matches_parser_help():
    text = SKILL.read_text(encoding="utf-8")
    tree = ast.parse(Path(comfy_render.__file__).read_text(encoding="utf-8"))
    expected_statuses, expected_keys = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            pairs = [(k.value, v) for k, v in zip(node.keys, node.values, strict=True)
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            status = next((v for k, v in pairs if k == "status"), None)
            if isinstance(status, ast.Constant) and isinstance(status.value, str):
                expected_statuses.add(status.value)
                expected_keys.update(k for k, _ in pairs)
        elif isinstance(node, ast.Call):
            status = next((k.value for k in node.keywords if k.arg == "status"), None)
            if isinstance(status, ast.Constant) and isinstance(status.value, str):
                expected_statuses.add(status.value)
                expected_keys.update(k.arg for k in node.keywords if k.arg)
    listed_statuses = set(re.findall(r"`([^`]+)`", re.search(r"^Statuses: (.+)$", text, re.M).group(1)))
    assert listed_statuses == expected_statuses
    listed_keys = set(re.findall(r"`([^`]+)`", re.search(r"^Keys: (.+)$", text, re.M).group(1)))
    assert listed_keys == expected_keys
