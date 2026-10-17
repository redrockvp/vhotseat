#!/usr/bin/env python3
"""gpu-workload as the guard node runs it, but against the test fakes: the real
CLI and arbiter, with the GPU and services read from the fake servers named in
GPU_WORKLOAD_STRATA_URL / GPU_WORKLOAD_COMFY_URL."""

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import os  # noqa: E402

from vhotseat import compat  # noqa: E402


def _vram(url):
    return json.load(urllib.request.urlopen(f"{url}/_vram"))["vram"]


class RemoteGpu:
    name = "remote"

    def snapshot(self, procs):
        used = {"strata_mib": _vram(os.environ["GPU_WORKLOAD_STRATA_URL"]),
                "comfy_mib": _vram(os.environ["GPU_WORKLOAD_COMFY_URL"]), "other_mib": 0}
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
    arb = compat.build(gpu=RemoteGpu(), machine_ram=machine_ram)
    for w in arb.workloads.values():
        w.service = AllUp()
    sys.exit(compat.main(arbiter=arb))
