"""The `vhotseat` CLI.

  vhotseat status [--json]                  who owns the GPU, what each workload holds, last event
  vhotseat switch NAME|none [--wait S] [--json]   give the GPU to NAME, or free it; idempotent
  vhotseat hold NAME [--wait S] -- CMD...   give it to NAME and keep it until CMD exits
  vhotseat hold NAME [--class C] [--for D] [--as R] [--queue-timeout S] [--wait S] -- CMD...
                                 the same, but --class makes it a ticket that waits for its turn
  vhotseat run NAME [--class C] [--for D] [--as R] [--wait S] [--queue-timeout S] -- CMD...
                                 run CMD once the queue serves its ticket (a request, not an order)
  vhotseat should-yield [--ticket ID] [--json]  has a run held the card past its class's max hold?
  vhotseat gate NAME                        for hook gates (a server's pre-load hook): may NAME load?
  vhotseat doctor [--json]                  check the config, the GPU and every workload and gate
  vhotseat doctor --exercise NAME [--wait S]   run a real cycle on NAME (take, load, release), timed
  vhotseat web [--host H] [--port P]        a dashboard: RAM and VRAM over time, handovers, events, logs
  vhotseat record                           sample RAM and VRAM into the telemetry until stopped
  vhotseat autostart ACTION                 install|status|remove: keep web (or record) up from logon
  vhotseat cost [--since 24h]               what the card's power has cost (needs power.price_per_kwh)
  vhotseat why [NAME]                       why a workload can or can't take the GPU right now
  vhotseat claims                           what owns the card by right (a game), and what matches now
  vhotseat watch                            free the card for a claim, record it, and sample, until stopped
  vhotseat book OWNER FROM TO [options]     reserve a window for new loads
  vhotseat unbook ID | pause ID | resume ID  remove, pause or resume a booking
  vhotseat bookings | queue [--json]        show windows or tickets
  vhotseat when NAME [options]              find when a workload can start

Exit codes: 0 ok · 1 failed / refused · 2 usage or bad config. `hold` exits with
CMD's own exit code once CMD has started. `autostart status` exits 1 when nothing
is registered.
"""

import argparse
import contextlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time

from . import claims, policy, schedule, util
from . import config as cfgmod
from .arbiter import NONE, TICKET_ENV, Arbiter
from .setup import build
from .telemetry import Sampler

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


def _autostart_name(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", value):
        raise argparse.ArgumentTypeError("name must be 1-32 lowercase letters, digits or hyphens, starting with a letter or digit")
    return value


def build_parser() -> argparse.ArgumentParser:
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
    sp.add_argument("--class", dest="cls",
                    help="queue a ticket of this class and wait for its turn, as `run` does "
                         "(without it a hold is an order, as always)")
    sp.add_argument("--for", dest="duration", help="the run's expected length (90m, 2h): the ticket's estimate")
    sp.add_argument("--as", dest="requester", help="who is asking, recorded on the ticket")
    sp.add_argument("--queue-timeout", type=float, default=0, metavar="SECONDS",
                    help="how long the ticket waits for its turn before giving up (default: no limit)")
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
    sp = sub.add_parser("proxy", help="gate a workload's HTTP server")
    sp.add_argument("name")
    sp.add_argument("--upstream", required=True)
    sp.add_argument("--port", type=int, required=True)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--hold", type=float, default=120.0)
    sub.add_parser("record", help="sample RAM and VRAM into the telemetry until stopped (for a service)")
    sub.add_parser("watch", help="free the card for a claim (a game) or a load that skipped its gate, "
                                 "record it, and sample, until stopped")
    sp = sub.add_parser("claims", help="what owns the card by right, and what matches right now")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("autostart", help="keep the dashboard and sampling up from logon")
    sp.add_argument("action", choices=("install", "status", "remove"))
    sp.add_argument("--name", dest="autostart_name", type=_autostart_name, default="vhotseat")
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
    sp = sub.add_parser("book", help="reserve a window for new loads")
    sp.add_argument("owner")
    sp.add_argument("start")
    sp.add_argument("end")
    sp.add_argument("--workload", action="append", default=[])
    sp.add_argument("--class", dest="cls", choices=schedule.BOOKING_CLASSES, default="booked")
    sp.add_argument("--every", choices=schedule.EVERY)
    sp.add_argument("--mode", choices=schedule.MODES, default="drain")
    sp.add_argument("--note", default="")
    sp.add_argument("--json", action="store_true")
    for cmd, what in (("unbook", "delete a booking"),
                      ("pause", "switch a booking off until TIME (default: the end of its current or next window)"),
                      ("resume", "switch a paused booking back on")):
        sp = sub.add_parser(cmd, help=what)
        sp.add_argument("id", type=int)
        if cmd == "pause":
            sp.add_argument("--until", metavar="TIME", help="HH:MM, or YYYY-MM-DD HH:MM")
    for cmd, what in (("bookings", "the active and upcoming booked windows (next 7 days)"),
                      ("queue", "waiting and running tickets, in the order they'd be served")):
        sp = sub.add_parser(cmd, help=what)
        sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("when", help="find when a workload can start")
    sp.add_argument("name")
    sp.add_argument("--class", dest="cls")
    sp.add_argument("--for", dest="duration")
    sp.add_argument("--as", dest="requester")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("run", help="run a command once the queue serves its ticket")
    sp.add_argument("name")
    sp.add_argument("--class", dest="cls", default="batch", help="the ticket's class (default batch)")
    sp.add_argument("--for", dest="duration", help="the run's expected length (90m, 2h): the ticket's estimate")
    sp.add_argument("--as", dest="requester", help="who is asking, recorded on the ticket")
    sp.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                    help="wait this long for running work to finish once it is your turn")
    sp.add_argument("--queue-timeout", type=float, default=0, metavar="SECONDS",
                    help="how long to wait for your turn before giving up (default: no limit)")
    sp.add_argument("command", nargs="*", metavar="-- COMMAND...")
    sp = sub.add_parser("should-yield",
                        help="has this run held the card past its class's max hold, with someone waiting?")
    sp.add_argument("--ticket", type=int, metavar="ID",
                    help="the ticket to ask about (default: $VHOTSEAT_TICKET, set by `run`)")
    sp.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None, arbiter: Arbiter | None = None, cfg: dict | None = None) -> int:
    util.LOG_PREFIX = "vhotseat"
    util.safe_output()
    p = build_parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    startup_command = []
    if len(argv) >= 2 and argv[0] == "autostart":
        i = 2
        while i < len(argv):
            if argv[i] in ("--name", "--command"):
                i += 2
            elif argv[i].startswith("--name=") or argv[i].startswith("--command="):
                i += 1
            else:
                startup_command, argv = argv[i:], argv[:i]
                break
    # hold's COMMAND is everything after the first `--`, split off here: a REMAINDER
    # argument would also swallow options before it (`hold comfy --wait 600 -- CMD`).
    command = argv[argv.index("--") + 1:] if "--" in argv else None
    args = p.parse_args(argv[:argv.index("--")] if command is not None else argv)
    if args.cmd == "autostart":
        args.startup_command = startup_command
    if command is not None:
        args.command = command

    if arbiter is None:
        try:
            # Only a config this process read itself is a file it may write back: a caller that
            # hands in a dict (a test, the library) gets a dashboard that can show the policy and
            # refuses to save one.
            path = cfgmod.default_path()
            ours = cfg is None
            cfg = cfg or cfgmod.load(path)
            arbiter = build(cfg, config_path=path if ours else None)
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

    if args.cmd in ("book", "unbook", "pause", "resume", "bookings", "queue", "when"):
        sc = arbiter.schedule
        if sc is None:
            if args.cmd in ("bookings", "queue", "when"):
                if json_out:
                    print(json.dumps({"now": True, "start": None, "bookings": [],
                                      "reason": "no schedule configured"} if args.cmd == "when" else []))
                else:
                    print("no schedule configured: nothing is booked")
                return EXIT_OK
            util.log("add a schedule: block to the config; see docs/reference.md")
            return EXIT_USAGE
        try:
            now = sc.now()
            if args.cmd == "book":
                unknown = set(args.workload) - set(arbiter.workloads)
                if unknown:
                    util.log(f"unknown workload {', '.join(sorted(unknown))}; configured: "
                             f"{', '.join(sorted(arbiter.workloads))}")
                    return EXIT_USAGE
                start, end = schedule.parse_time(args.start, now), schedule.parse_time(args.end, now)

                def hhmm(text):
                    return len(text.split(":")) == 2 and "-" not in text
                if end <= start and hhmm(args.end):
                    end = schedule._shift_days(end, 1)       # 22:00 01:00: the next morning
                if not args.every and end <= now and hhmm(args.start) and hhmm(args.end):
                    # Today's window has gone: at 23:00, `01:00 03:00` means tonight.
                    start, end = schedule._shift_days(start, 1), schedule._shift_days(end, 1)
                if not args.every and end <= now:
                    raise ValueError("a one-off booking cannot end in the past")
                row = sc.add_booking(args.owner, start, end, cls=args.cls, workloads=args.workload or ["*"],
                                     every=args.every, mode=args.mode, note=args.note)
                arbiter.events.record("booking", booking=row["id"], owner=row["owner"])
                print(json.dumps(row) if json_out else f"booking {row['id']} ({row['owner']})")
            elif args.cmd in ("unbook", "pause", "resume"):
                if args.cmd == "unbook":
                    row = sc.remove_booking(args.id)
                elif args.cmd == "pause":
                    until = schedule.parse_time(args.until, now) if args.until else None
                    if until is not None and until <= now:
                        raise ValueError(f"--until {args.until} has already passed")
                    row = sc.pause(args.id, until)
                else:
                    row = sc.resume(args.id)
                event = {"unbook": "unbook", "pause": "booking-pause", "resume": "booking-resume"}[args.cmd]
                arbiter.events.record(event, booking=row["id"], owner=row["owner"])
                print(f"{event}: booking {row['id']} ({row['owner']})")
            elif args.cmd == "bookings":
                rows = []
                for b in sc.bookings():
                    windows = sc.windows(b, now, now + 7 * schedule.DAY)
                    if windows:
                        rows.append({**b, "next_window": {"start": windows[0][0], "end": windows[0][1]}})
                if json_out:
                    print(json.dumps(rows))
                elif not rows:
                    print("nothing booked in the next 7 days")
                for b in ([] if json_out else rows):
                    w = b["next_window"]
                    print(f"{b['id']} {b['owner']} {b['class']} {','.join(b['workloads'])} "
                          f"{schedule.clock(w['start'])}-{schedule.clock(w['end'])} "
                          f"every={b['every'] or '-'} mode={b['mode']} "
                          f"paused-until={schedule.clock(b['paused_until']) if b['paused_until'] else '-'} "
                          f"{b['note']}")
            elif args.cmd == "queue":
                rows = sc.queue_view()
                if json_out:
                    print(json.dumps(rows))
                for t in ([] if json_out else rows):
                    print(f"{t['state']} {t['class']} {t['requester']} {t['workload']} "
                          f"age={int(now - t['created'])}s estimate={t['est_s']}")
            else:
                if args.cls and args.cls not in sc.classes:
                    raise ValueError(f"unknown class {args.cls!r}; configured: {', '.join(sc.classes)}")
                duration = util.window(args.duration) if args.duration else 0
                result = sc.when(name, args.cls, duration, args.requester)
                if json_out:
                    print(json.dumps(result))
                elif result["now"]:
                    print(result["reason"])
                else:
                    print(f"{schedule.clock(result['start']) if result['start'] else 'no start found'}: "
                          f"{result['reason']}")
                return EXIT_OK if result["now"] else EXIT_FAIL
        except schedule.ScheduleError as e:
            util.log(str(e))
            return EXIT_FAIL
        except (sqlite3.Error, OSError, json.JSONDecodeError) as e:
            util.log(f"schedule unreadable ({arbiter.schedule.path}): {e}")
            return EXIT_FAIL
        except ValueError as e:
            util.log(str(e))
            return EXIT_USAGE
        return EXIT_OK
    if args.cmd == "status":
        emit(arbiter.probe())
        return EXIT_OK
    if args.cmd == "gate":
        return EXIT_OK if arbiter.gate(name) else EXIT_FAIL
    if args.cmd in ("hold", "run"):
        command = args.command
        if not command:
            p.error(f"{args.cmd} needs a command after --")
        cls = getattr(args, "cls", None)
        if cls and arbiter.schedule is not None and cls not in arbiter.schedule.classes:
            util.log(f"unknown class {cls!r}; configured: {', '.join(arbiter.schedule.classes)}")
            return EXIT_USAGE
        try:
            est_s = util.window(args.duration) if args.duration else None
        except ValueError as e:
            util.log(str(e))
            return EXIT_USAGE
        # A hold can last hours, and a ticket can wait for hours before it runs: sample either way.
        with Sampler(arbiter):
            if args.cmd == "run" or cls:
                # --class on a hold makes it a ticket: a typed `hold` without one is still an order.
                return arbiter.run(name, command, args.wait, cls=cls or "batch", est_s=est_s,
                                   requester=args.requester, queue_timeout=args.queue_timeout)
            return arbiter.hold(name, command, args.wait)
    if args.cmd == "should-yield":
        tid = args.ticket if args.ticket is not None else os.environ.get(TICKET_ENV)
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            util.log("no ticket to ask about: pass --ticket ID, or run inside `vhotseat run`")
            return EXIT_USAGE
        if arbiter.schedule is None:
            util.log("no schedule configured: a run never yields the card")
            return EXIT_FAIL
        o = arbiter._schedule_read(lambda sc: sc.overdue(tid))
        if json_out:
            print(json.dumps({"yield": True, **o} if o else {"yield": False}))
        elif o:
            print(f"yield: held {int(o['held_s'] // 60)} min, past the class's "
                  f"{int(o['limit_s'] // 60)} min; {o['waiting']} waiting")
        else:
            print("carry on")
        return EXIT_OK if o else EXIT_FAIL
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
            command = (subprocess.list2cmdline(args.startup_command) if os.name == "nt"
                       else shlex.join(args.startup_command)) if args.startup_command else args.command
            return autostart.install(command, name=args.autostart_name)
        if args.action == "remove":
            return autostart.remove(name=args.autostart_name)
        return autostart.status(name=args.autostart_name)
    if args.cmd == "proxy":
        from . import proxy
        return proxy.serve(arbiter, name, args.upstream, args.port, args.hold, args.host)
    if args.cmd == "claims":
        hits = arbiter.claim_hits()
        if json_out:
            print(json.dumps({"hits": hits,
                              "rules": [{k: v for k, v in vars(r).items() if v} for r in arbiter.claim_rules]}))
        else:
            if hits:
                free = any(h.get("on_start", "release") == "release" for h in hits)
                print(f"the card is claimed by {claims.describe(hits)}: "
                      + ("unloading what is loaded to free it, and nothing new loads while it runs"
                         if free else "nothing new loads while it runs"))
                for h in hits[:8]:
                    print(f"{'':<12}- pid {h['pid']}: {h['by']}")
                    if h["exe"]:
                        print(f"{'':<12}  {h['exe']}")
                if len(hits) > 8:
                    print(f"{'':<12}- and {len(hits) - 8} more processes")
                hit_keys = {(h["name"], h["note"]) for h in hits}
                for key, rec in sorted(arbiter.events.claims_open().items()):
                    if key not in hit_keys:
                        continue
                    bits = []
                    if rec["released"]:
                        bits.append("unloaded " + ", ".join(rec["released"])
                                    + (f" ({rec['freed_mib']} MiB freed)" if rec.get("freed_mib") else ""))
                    bits += [f"{w['workload']} keeps its model: {w['why']}" for w in rec["waiting"]]
                    if kept := rec.get("kept"):
                        bits.append("kept for its policy: " + ", ".join(kept))
                    if bits:
                        print(f"{'':<12}for {key[0]} ({key[1]}): " + "; ".join(bits))
            else:
                print("nothing claims the card right now")
            print(f"rules ({len(arbiter.claim_rules)}):")
            for r in arbiter.claim_rules:
                where, what = next((w, getattr(r, w)) for w in ("name", "path", "parent") if getattr(r, w))
                print(f"{'':<12}- {where} {what}" + (f"  ({r.note})" if r.note else "")
                      + f"  -> on_start: {r.on_start}")
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
            for w in why.values():
                print(f"{w['label'] + ':':<12}{word[w['would']]} - priority {w['priority']}, "
                      f"gives way for {policy.describe(w['yields_to'], w.get('asks_for', ()))}")
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
        report = cost(tel.samples(now - span, now), price, step_s=tel.interval_s)
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
