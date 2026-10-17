#!/usr/bin/env python3
"""vhotseat as the guard node runs it, but against the test fakes: the real CLI
and arbiter on config.example.yaml, with the URLs from STRATA_URL / COMFY_URL
and the GPU and services read from the fake servers."""

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import example_config  # noqa: E402
from vhotseat import cli, setup  # noqa: E402


def _vram(url):
    return json.load(urllib.request.urlopen(f"{url}/_vram"))["vram"]


class RemoteGpu:
    name = "remote"

    def snapshot(self, selectors):
        used = {"strata_mib": _vram(os.environ["STRATA_URL"]), "comfy_mib": _vram(os.environ["COMFY_URL"]),
                "other_mib": 0}
        return {"total_mib": 16303, "free_mib": 16303 - sum(used.values()), **used}


class AllUp:
    unit = "fake.service"

    def is_active(self):
        return True

    def memory(self):
        return {"ram_mib": 100, "swap_mib": 0, "file_mib": 0}


def machine_ram():
    return {"total_mib": 61952, "available_mib": 50000, "swap_total_mib": 8192, "swap_used_mib": 0}


if __name__ == "__main__":
    cfg = example_config(os.environ["STRATA_URL"], os.environ["COMFY_URL"])
    arb = setup.build(cfg, gpu=RemoteGpu(), machine_ram=machine_ram)
    for w in arb.workloads.values():
        w.service = AllUp()
    sys.exit(cli.main(arbiter=arb, cfg=cfg))
