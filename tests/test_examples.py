"""examples/*.yaml are complete, valid configs: people copy them."""

from pathlib import Path

import pytest

from vhotseat import config

EXAMPLES = sorted((Path(__file__).resolve().parent.parent / "examples").glob("*.yaml"))


def test_there_are_examples():
    assert len(EXAMPLES) >= 3


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_validates(path):
    config.parse(path.read_text())


def test_examples_combine():
    """Their workloads go under one `workloads:` key without clashing."""
    merged = {"workloads": {}}
    for path in EXAMPLES:
        wls = config.parse(path.read_text())["workloads"]
        assert not set(wls) & set(merged["workloads"])
        merged["workloads"].update(wls)
    config.validate(merged)
