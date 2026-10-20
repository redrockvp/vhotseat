"""The `vhotseat` CLI.

  vhotseat status [--json]                  who owns the GPU, what each workload holds, last event
  vhotseat switch NAME|none [--wait S] [--json]   give the GPU to NAME, or free it; idempotent
  vhotseat hold NAME [--wait S] -- CMD...   give it to NAME and keep it until CMD exits
  vhotseat gate NAME                        for hook gates (a server's pre-load hook): may NAME load?
  vhotseat doctor [--json]                  check the config, the GPU and every workload and gate
  vhotseat doctor --exercise NAME [--wait S]   run a real cycle on NAME (take, load, release), timed
  vhotseat web [--host H] [--port P]        a dashboard: RAM and VRAM over time, handovers, events, logs
  vhotseat record                           sample RAM and VRAM into the telemetry until stopped
  vhotseat autostart ACTION                 install|status|remove: keep web (or record) up from logon
  vhotseat cost [--since 24h]               what the card's power has cost (needs power.price_per_kwh)
  vhotseat why [NAME]                       why a workload can or can't take the GPU right now
  vhotseat claims                           what owns the card by right (a game), and what matches now
  vhotseat watch                            note when a claim starts and ends, and sample, until stopped

Exit codes: 0 ok · 1 failed / refused · 2 usage or bad config. `hold` exits with
CMD's own exit code once CMD has started. `autostart status` exits 1 when nothing
is registered.
"""

import argparse
import contextlib
import json
import sys
import time

from . import claims
from . import config as cfgmod
from . import util
from .arbiter import NONE, Arbiter
from .setup import build
from .telemetry import Sampler

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


def main(argv: list[str] | None = None, arbiter: Arbiter | None = None, cfg: dict | None = None) -> int:
    util.LOG_PREFIX = "vhotseat"
    util.safe_output()
    p = argparse.ArgumentParser(prog="vhotseat", description="Take turns on one GPU.",
                                epilog=f"config: {cfgmod.default_path()} (or $VHOTSEAT_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("status", help="who owns the GPU")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("switch", help="give the GPU to a workload, or free it (none); idempotent")
    sp.add_argument("name")
    sp.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                    help="wait this long for running work to finish instead of failing")
    sp.add_argument("--request", action="store_true",
                    help="this switch is a workload asking for someone else (ComfyUI's guard): a "
                         "claimed process refuses it, a switch you typed yourself does not")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("hold", help="give the GPU to a workload and keep it until COMMAND exits")
    sp.add_argument("name")
    sp.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                    help="wait this long for running work to finish instead of failing")
    # Not REMAINDER: that swallows `--wait 600` after NAME even once the command is
    # split off below, and the wait silently becomes 0.
    sp.add_argument("command", nargs="*", metavar="-- COMMAND...")
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
    sub.add_parser("watch", help="record when a claim (a game) starts and ends, and sample, until stopped")
    sp = sub.add_parser("claims", help="what owns the card by right, and what matches right now")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("autostart", help="keep the dashboard and sampling up from logon")
    sp.add_argument("action", choices=("install", "status", "remove"))
    sp.add_argument("--command", choices=("web", "record", "watch"), default="web",
                    help="what starts at logon: web (the dashboard, which also samples), "
                         "record (sampling only) or watch (sampling plus claim events)")
    sp = sub.add_parser("cost", help="what the card's power has cost over a window")
    sp.add_argument("--since", default="24h", metavar="WINDOW",
                    help="how far back: 90m, 24h, 7d or seconds (default 24h)")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("why", help="why a workload can or can't take the GPU right now (reads only)")
    sp.add_argument("name", nargs="?", help="one workload; default: every one")
    sp.add_argument("--json", action="store_true")
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
    if args.cmd in ("web", "record", "watch"):
        from . import web
        if args.cmd == "web":
            return web.serve(arbiter, args.host, args.port)
        return web.record(arbiter) if args.cmd == "record" else web.watch(arbiter)
    if args.cmd == "autostart":
        from . import autostart
        if args.action == "install":
            return autostart.install(args.command)
        if args.action == "remove":
            return autostart.remove()
        return autostart.status()
    if args.cmd == "claims":
        hits = arbiter.claim_hits()
        if json_out:
            print(json.dumps({"hits": hits,
                              "rules": [{k: v for k, v in vars(r).items() if v} for r in arbiter.claim_rules]}))
        else:
            if hits:
                print(f"the card is claimed by {claims.describe(hits)}: nothing new loads while it runs")
                for h in hits[:8]:
                    print(f"{'':<12}- pid {h['pid']}: {h['by']}")
                    if h["exe"]:
                        print(f"{'':<12}  {h['exe']}")
                if len(hits) > 8:
                    print(f"{'':<12}- and {len(hits) - 8} more processes")
            else:
                print("nothing claims the card right now")
            print(f"rules ({len(arbiter.claim_rules)}):")
            for r in arbiter.claim_rules:
                where, what = next((w, getattr(r, w)) for w in ("name", "path", "parent") if getattr(r, w))
                print(f"{'':<12}- {where} {what}" + (f"  ({r.note})" if r.note else ""))
        return EXIT_OK
    if args.cmd == "why":
        why = arbiter.why_all()
        if args.name:
            why = {args.name: why[args.name]}
        if json_out:
            print(json.dumps(why))
        else:
            word = {"ready": "could take the card now", "waiting": "would have to wait",
                    "blocked": "is blocked"}
            for name, w in why.items():
                print(f"{w['label'] + ':':<12}{word[w['would']]}")
                for note in w["notes"]:
                    print(f"{'':<12}- {note}")
        return EXIT_OK
    if args.cmd == "cost":
        from .telemetry import cost
        price = arbiter.power.get("price_per_kwh")
        if not price:
            util.log("no power.price_per_kwh in the config: the card's watts are known, what they cost isn't")
            return EXIT_USAGE
        if arbiter.telemetry is None:
            util.log("telemetry is off in the config: there is no power history to cost out")
            return EXIT_USAGE
        try:
            span = util.window(args.since)
        except ValueError as e:
            util.log(str(e))
            return EXIT_USAGE
        tel = arbiter.telemetry
        now = time.time()
        report = cost(tel.samples(now - span, now), price)
        if json_out:
            print(json.dumps({"window_s": span, "price": price, **arbiter.power, **report}))
            return EXIT_OK

        def money(v): return f"{arbiter.power.get('currency', '')}{v:,.2f}"
        print(f"cost:   {money(report['cost'])} over the last {args.since}: {report['kwh']} kWh "
              f"at {money(price)} a kWh, {report['hours']} h on the card")
        if report["hours"] < span / 3600 * 0.9:
            # Wattage only exists where something was sampling: an unsampled stretch is no
            # data, not a card that drew nothing. Say which of the two this number is.
            util.log(f"only {report['hours']} h of that {args.since} window has power samples in it: "
                     "this is a floor, not the whole cost. `vhotseat autostart install` samples continuously.")
        for mode, cell in sorted(report["by_mode"].items(), key=lambda kv: -kv[1]["kwh"]):
            label = arbiter.workloads[mode].label if mode in arbiter.workloads else \
                ("free" if mode == "none" else mode)
            print(f"        {label:<12}{cell['kwh']:>7} kWh  {money(cell['cost']):>9}  {cell['hours']:>5} h")
        if (w := report["w_now"]) is not None:
            print(f"        card now {w} W ({money(w / 1000 * price)} an hour)")
        if note := arbiter.power.get("note"):
            print(f"        rate: {note}")
        util.log("that is the card's own draw: the rest of the machine, and the PSU burning some of "
                 "it on the way in, aren't in it")
        return EXIT_OK
    # A switch may wait minutes for running work to finish (the ComfyUI guard waits up
    # to 300 s): sample meanwhile.
    with Sampler(arbiter) if args.wait else contextlib.nullcontext():
        result = arbiter.switch(name, args.wait, request=args.request)
    result.pop("exception", None)
    if result.pop("inside_hold", None):
        return EXIT_FAIL
    emit(arbiter.probe(), result)
    return EXIT_OK if result["ok"] else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
