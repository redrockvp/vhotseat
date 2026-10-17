"""The `gpu-workload` CLI, unchanged for its callers.

Strata's before_load (`gpu-workload hook strata-before-load`), the ComfyUI
guard (`gpu-workload switch comfy --wait N`), a dashboard
(`status --json`, `switch comfy --json`) and people all call this. Its commands,
output and exit codes stay as they were.

It reads the vhotseat config when there is one (it must name its workloads
`strata` and `comfy`); without one it uses the setup gpu-workload always had,
with its GPU_WORKLOAD_* env vars. Either way it takes the same lock and writes
the same event log as `vhotseat`.
"""

import argparse
import json
import os
import sys

from . import config as cfgmod
from . import util
from .arbiter import Arbiter
from .setup import build as build_arbiter

DESCRIPTION = "gpu-workload: hand the single 16 GB GPU between Strata (local LLM) and ComfyUI."

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_WRONG_MODE = 0, 1, 2, 3


def builtin_config(env=os.environ) -> dict:
    """gpu-workload's setup before there was a config file."""
    return cfgmod.validate({
        "gpu": {"backend": "auto"},
        "workloads": {
            "strata": {
                "driver": "http", "label": "Strata",
                "url": env.get("GPU_WORKLOAD_STRATA_URL", "http://127.0.0.1:2335"),
                "service": {"systemd-user": env.get("GPU_WORKLOAD_STRATA_UNIT", "strata")},
                # nvidia-smi process paths that identify each server's GPU allocations.
                "vram": {"process": env.get("GPU_WORKLOAD_STRATA_PROC", "/Strata/")},
                "resident": {"get": "/health", "path": "loaded"},
                "release": {"post": "/unload?wait={wait}", "always": True,
                            "busy_when": [{"status": 409}, {"path": "status", "in": ["busy"]}],
                            "ok_when": [{"status": 200, "path": "status", "in": ["unloaded", "not loaded"]}]},
                "confirm": {"vram_at_most": 0},
                "load": {"post": "/load", "timeout": 300},
                "gate": {"kind": "hook", "timeout": 90},
            },
            "comfy": {
                "driver": "comfyui", "label": "ComfyUI",
                "url": env.get("GPU_WORKLOAD_COMFY_URL", "http://127.0.0.1:8188"),
                "service": {"systemd-user": env.get("GPU_WORKLOAD_COMFY_UNIT", "comfyui")},
                "vram": {"process": env.get("GPU_WORKLOAD_COMFY_PROC", "/ComfyUI/")},
                "idle_vram_mib": int(env.get("GPU_WORKLOAD_COMFY_IDLE_MIB", "1500")),
                "restart_above_ram_mib": int(env.get("GPU_WORKLOAD_COMFY_RESTART_MIB", "4096")),
                "allow_restart": True,
                "gate": {"kind": "plugin", "timeout": 300},
            },
        },
    })


def build(env=os.environ, **kw) -> Arbiter:
    path = cfgmod.default_path(env)
    cfg = cfgmod.load(path) if path.exists() else builtin_config(env)
    missing = {"strata", "comfy"} - set(cfg["workloads"])
    if missing:
        raise cfgmod.ConfigError(f"{path}: gpu-workload needs workloads named strata and comfy "
                                 f"(missing: {', '.join(sorted(missing))}); use `vhotseat` instead")
    # The hook must finish inside Strata's before_load_timeout_s (90 s): /free ~2 s
    # (or this timeout when it can't empty VRAM), then possibly the history settle
    # (<= 10 s) and a ComfyUI restart (~15-20 s, up to its restart timeout, 50 s).
    return build_arbiter(cfg, env=env, idle_owner="comfy", gate_release_timeout_s=20,
                         hold_env="GPU_WORKLOAD_HOLD", cli="gpu-workload", **kw)


def main(argv: list[str] | None = None, arbiter: Arbiter | None = None) -> int:
    util.LOG_PREFIX = "gpu-workload"
    p = argparse.ArgumentParser(prog="gpu-workload", description=DESCRIPTION)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("status", "show the current mode"),
                        ("switch", "move the GPU to a workload (idempotent)"),
                        ("check", "exit 0 if in MODE, 3 otherwise"),
                        ("hold", "switch to MODE and keep it until COMMAND exits")):
        sp = sub.add_parser(name, help=help_)
        if name != "hold":
            sp.add_argument("--json", action="store_true", help="machine-readable output on stdout")
        if name == "hold":
            sp.add_argument("mode", choices=("comfy",))
        elif name != "status":
            sp.add_argument("mode", choices=("strata", "comfy"))
        if name in ("switch", "hold"):
            sp.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                            help="wait this long for running work to finish instead of failing")
    sub.choices["hold"].add_argument("command", nargs=argparse.REMAINDER, metavar="-- COMMAND...")
    hp = sub.add_parser("hook", help="entry points for server hooks")
    hp.add_argument("name", choices=("strata-before-load",))
    args = p.parse_args(argv)
    json_out = getattr(args, "json", False)
    try:
        arb = arbiter or build()
    except cfgmod.ConfigError as e:
        util.log(str(e))
        # A broken config must not let Strata load unchecked: the hook refuses.
        return EXIT_FAIL if args.cmd in ("hook", "switch", "hold") else EXIT_USAGE

    def emit(s: dict, extra: dict | None = None) -> None:
        print(json.dumps({**s, **(extra or {})}) if json_out else arb.describe(s))

    if args.cmd == "hook":
        return EXIT_OK if arb.gate("strata") else EXIT_FAIL

    if args.cmd == "hold":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            p.error("hold needs a command after --")
        return arb.hold(args.mode, command, args.wait)

    if args.cmd == "status":
        emit(arb.probe())
        return EXIT_OK

    if args.cmd == "check":
        s = arb.probe()
        # comfy mode with ComfyUI down is no use to a comfy workflow.
        ok = s["mode"] == args.mode and (args.mode != "comfy" or s["comfy"]["reachable"])
        emit(s, {"ok": ok, "want": args.mode})
        if not ok and not json_out:
            util.log(f"GPU is in {s['mode']} mode, not {args.mode}; run `gpu-workload switch {args.mode}`")
        return EXIT_OK if ok else EXIT_WRONG_MODE

    result = arb.switch(args.mode, args.wait)
    if result.pop("inside_hold", None):
        return EXIT_FAIL
    emit(arb.probe(), result)
    return EXIT_OK if result["ok"] else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
