"""The `vhotseat` CLI.

  vhotseat status [--json]                  who owns the GPU, what each workload holds, last event
  vhotseat switch NAME|none [--wait S] [--json]   give the GPU to NAME, or free it; idempotent
  vhotseat hold NAME [--wait S] -- CMD...   give it to NAME and keep it until CMD exits
  vhotseat gate NAME                        for hook gates (a server's pre-load hook): may NAME load?
  vhotseat doctor [--json]                  check the config, the GPU and every workload and gate
  vhotseat doctor --exercise NAME [--wait S]   run a real cycle on NAME (take, load, release), timed
  vhotseat web [--host H] [--port P]        a dashboard: RAM and VRAM over time, handovers, events, logs
  vhotseat record                           sample RAM and VRAM into the telemetry until stopped

Exit codes: 0 ok · 1 failed / refused · 2 usage or bad config. `hold` exits with
CMD's own exit code once CMD has started.
"""

import argparse
import contextlib
import json
import sys

from . import config as cfgmod
from . import util
from .arbiter import NONE, Arbiter
from .setup import build
from .telemetry import Sampler

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


def main(argv: list[str] | None = None, arbiter: Arbiter | None = None, cfg: dict | None = None) -> int:
    util.LOG_PREFIX = "vhotseat"
    p = argparse.ArgumentParser(prog="vhotseat", description="Take turns on one GPU.",
                                epilog=f"config: {cfgmod.default_path()} (or $VHOTSEAT_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("status", help="who owns the GPU")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("switch", help="give the GPU to a workload, or free it (none); idempotent")
    sp.add_argument("name")
    sp.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                    help="wait this long for running work to finish instead of failing")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("hold", help="give the GPU to a workload and keep it until COMMAND exits")
    sp.add_argument("name")
    sp.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                    help="wait this long for running work to finish instead of failing")
    sp.add_argument("command", nargs=argparse.REMAINDER, metavar="-- COMMAND...")
    sp = sub.add_parser("gate", help="a workload's pre-load hook: exit 0 to let it load, 1 to refuse")
    sp.add_argument("name")
    sp = sub.add_parser("doctor", help="check the config, the GPU, and every workload and gate")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--exercise", metavar="NAME",
                    help="run a real cycle on NAME: take the GPU, load, release, confirm (it unloads things)")
    sp.add_argument("--wait", type=float, default=60, metavar="SECONDS",
                    help="with --exercise: how long to wait for running work")
    sp = sub.add_parser("web", help="serve the dashboard: RAM and VRAM over time, handovers, events, logs")
    sp.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    sp.add_argument("--port", type=int, default=8765, help="port (default 8765)")
    sub.add_parser("record", help="sample RAM and VRAM into the telemetry until stopped (for a service)")
    argv = sys.argv[1:] if argv is None else list(argv)
    # hold's COMMAND is everything after the first `--`, split off here: a REMAINDER
    # argument would also swallow options before it (`hold comfy --wait 600 -- CMD`).
    command = argv[argv.index("--") + 1:] if "--" in argv else None
    args = p.parse_args(argv[:argv.index("--")] if command is not None else argv)
    if command is not None:
        args.command = command

    if arbiter is None:
        try:
            cfg = cfg or cfgmod.load(cfgmod.default_path())
            arbiter = build(cfg)
        except cfgmod.ConfigError as e:
            util.log(str(e))
            # A broken config must not let a workload load unchecked: the gate refuses.
            return EXIT_FAIL if args.cmd == "gate" else EXIT_USAGE

    names = set(arbiter.workloads)
    name = getattr(args, "name", None) or getattr(args, "exercise", None)
    if name is not None and name not in names and not (args.cmd == "switch" and name == NONE):
        util.log(f"no workload named {name!r}; configured: {', '.join(sorted(names))}")
        return EXIT_FAIL if args.cmd == "gate" else EXIT_USAGE

    tel = arbiter.telemetry
    if tel is not None:
        tel.source = " ".join(x for x in (args.cmd, name) if x)
        util.LOG_SINK = tel.log
    try:
        return _run(p, args, arbiter, cfg, name)
    finally:
        util.LOG_SINK = None


def _run(p, args, arbiter: Arbiter, cfg: dict | None, name: str | None) -> int:
    json_out = getattr(args, "json", False)

    def emit(s: dict, extra: dict | None = None) -> None:
        print(json.dumps({**s, **(extra or {})}) if json_out else arbiter.describe(s))

    if args.cmd == "status":
        emit(arbiter.probe())
        return EXIT_OK
    if args.cmd == "gate":
        return EXIT_OK if arbiter.gate(name) else EXIT_FAIL
    if args.cmd == "hold":
        command = args.command
        if not command:
            p.error("hold needs a command after --")
        # A hold can last hours: keep sampling while it runs.
        with Sampler(arbiter):
            return arbiter.hold(name, command, args.wait)
    if args.cmd == "doctor":
        from .doctor import exercise, run
        if args.exercise:
            return exercise(arbiter, args.exercise, args.wait, json_out)
        return run(arbiter, cfg, json_out)
    if args.cmd in ("web", "record"):
        from . import web
        return web.serve(arbiter, args.host, args.port) if args.cmd == "web" else web.record(arbiter)
    # A switch may wait minutes for running work to finish (the ComfyUI guard waits up
    # to 300 s): sample meanwhile.
    with Sampler(arbiter) if args.wait else contextlib.nullcontext():
        result = arbiter.switch(name, args.wait)
    result.pop("exception", None)
    if result.pop("inside_hold", None):
        return EXIT_FAIL
    emit(arbiter.probe(), result)
    return EXIT_OK if result["ok"] else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
