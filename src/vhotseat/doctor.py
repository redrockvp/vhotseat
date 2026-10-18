"""`vhotseat doctor`: check that the guarantees actually hold on this machine.

Gates live outside vhotseat: a line in a server's config, a node in ComfyUI's
custom_nodes. If one is missing nothing fails; the GPU just stops being
guarded. So doctor checks every gate is installed and pointing here, as well
as the config, the GPU backend and each workload. Read-only: it never
switches, loads or unloads anything.
"""

import json
import re
import shutil
from pathlib import Path

from . import util

OK, WARN, FAIL = "ok", "warn", "fail"


def _gate_hook(w, gate: dict) -> list:
    """A hook gate: the server's config runs `vhotseat gate NAME` before it loads,
    strictly, with a limit that fits our release time."""
    out = []
    path = gate.get("config")
    if not path:
        return [(WARN, f"{w.name}: gate", "no gate.config set, so the hook can't be checked")]
    p = Path(path).expanduser()
    try:
        cfg = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return [(FAIL, f"{w.name}: gate", f"can't read {p}: {e}")]
    cmd = cfg.get("before_load")
    cmd = cmd.split() if isinstance(cmd, str) else cmd or []
    calls_us = len(cmd) >= 3 and Path(cmd[0]).name == "vhotseat" and cmd[1:3] == ["gate", w.name]
    if not calls_us:
        out.append((FAIL, f"{w.name}: gate", f"{p} before_load is {cmd or 'unset'}: "
                    f"loads aren't guarded; set it to [\"vhotseat\", \"gate\", \"{w.name}\"]"))
    elif not shutil.which(cmd[0]):
        out.append((FAIL, f"{w.name}: gate", f"before_load runs {cmd[0]}, which isn't there"))
    else:
        out.append((OK, f"{w.name}: gate", f"{p.name} runs `{' '.join(cmd)}` before loading"))
    if not cfg.get("before_load_strict"):
        out.append((FAIL, f"{w.name}: gate", "before_load_strict isn't true: a refused load would load anyway"))
    limit, need = cfg.get("before_load_timeout_s", 120), gate.get("timeout")
    if need and limit < need:
        out.append((WARN, f"{w.name}: gate", f"before_load_timeout_s is {limit}, gate.timeout is {need}: "
                    "the server may kill the hook while it's still making room"))
    return out


def _gate_plugin(w, gate: dict) -> list:
    """A plugin gate (the ComfyUI guard node): installed, and turned on."""
    nodes = gate.get("custom_nodes")
    if not nodes:
        return [(WARN, f"{w.name}: gate", "no gate.custom_nodes set, so the guard can't be checked")]
    guard = Path(nodes).expanduser() / "vhotseat_guard"
    if not (guard / "__init__.py").exists():
        return [(FAIL, f"{w.name}: gate", f"guard node not installed in {nodes}: jobs aren't guarded")]
    return [(OK, f"{w.name}: gate", f"guard node installed ({guard.resolve()})")]


def _version_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _installed_version(path: Path) -> str | None:
    """`version = "X.Y.Z"` (pyproject.toml and the like), or None."""
    try:
        m = re.search(r"""^\s*version\s*=\s*["']([^"']+)["']""", path.read_text(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def check_patches(cfg: dict | None) -> list:
    """Local patches to other projects' code (config `patches:`). An update of
    that project silently drops a patch, and the symptom (a release that leaves
    VRAM behind, every handover refused) shows up far from the cause: catch it
    here instead. Once upstream ships the fix, say the patch can go."""
    out = []
    for p in (cfg or {}).get("patches") or []:
        where = f"patch: {p['name']}"
        f = Path(p["file"]).expanduser()
        try:
            present = p["marker"] in f.read_text()
        except OSError as e:
            out.append((FAIL, where, f"can't read {f}: {e.strerror}"))
            continue
        fixed = p.get("fixed_upstream_in")
        have = _installed_version(Path(fixed["version_file"]).expanduser()) if fixed else None
        upstream_has_it = bool(have and _version_tuple(have) >= _version_tuple(fixed["version"]))
        doc = f"; see {p['doc']}" if p.get("doc") else ""
        if present and upstream_has_it:
            out.append((WARN, where, f"still applied, but version {have} has the fix upstream "
                        f"(since {fixed['version']}): the local patch can go{doc}"))
        elif present:
            out.append((OK, where, f"applied ({f.name})" + (f"; upstream has it from {fixed['version']}, "
                                                             f"installed {have}" if fixed else "")))
        elif upstream_has_it:
            out.append((OK, where, f"not needed: version {have} has the fix upstream; remove this entry"))
        else:
            out.append((FAIL, where, f"missing from {f}: an update dropped it; re-apply it{doc}"))
    return out


def checks(arbiter, cfg: dict | None) -> list:
    out = []
    g = arbiter.snapshot()
    if "error" in g:
        out.append((FAIL, "gpu", g["error"]))
    else:
        out.append((OK, "gpu", f"{arbiter.gpu.name}: {g['free_mib']} of {g['total_mib']} MiB free"))
    ram = arbiter.machine_ram()
    out += check_patches(cfg)
    out.append((FAIL, "ram", ram["error"]) if "error" in ram else
               (OK, "ram", f"{ram['available_mib']} of {ram['total_mib']} MiB available"))
    for w in arbiter.workloads.values():
        if w.service.unit:
            active = w.service_active()
            out.append((OK if active else WARN, f"{w.name}: service",
                        f"{w.service.unit} {'active' if active else 'stopped (started on demand)'}"))
            if not active:
                continue
        code, _ = util.http("GET", w.url + w.health_path)
        out.append((OK if code == 200 else FAIL, f"{w.name}: api",
                    f"{w.url}{w.health_path} answered {code}" if code else f"{w.url} not answering"))
        kind = w.gate.get("kind")
        if kind == "hook":
            out += _gate_hook(w, w.gate)
        elif kind == "plugin":
            out += _gate_plugin(w, w.gate)
        else:
            out.append((OK, f"{w.name}: gate", "hold: guarded only while run under `vhotseat hold`"))
        per = w.admission.get("by_model") or {}
        sets = [("", w.admission), *((f" ({m})", lim) for m, lim in per.items())]
        for label, adm in sets:
            if (need := adm.get("min_free_vram_mib")) and g.get("total_mib") and need > g["total_mib"]:
                out.append((FAIL, f"{w.name}: admission", f"min_free_vram_mib{label} {need} is more than the card has"))
            if (need := adm.get("min_available_ram_mib")) and ram.get("total_mib") and need > ram["total_mib"]:
                out.append((FAIL, f"{w.name}: admission",
                            f"min_available_ram_mib{label} {need} is more than the machine has"))
        if per and w.service_active():
            model = w.model()
            if model is None:
                out.append((WARN, f"{w.name}: admission", "can't tell which model it serves; using the defaults"))
            elif model not in per:
                out.append((WARN, f"{w.name}: admission",
                            f"serves {model}, which isn't in by_model; using the defaults"))
            else:
                out.append((OK, f"{w.name}: admission", f"serves {model}: {per[model]}"))
    return out


def run(arbiter, cfg: dict | None, json_out: bool) -> int:
    results = checks(arbiter, cfg)
    if json_out:
        print(json.dumps([{"status": s, "check": c, "detail": d} for s, c, d in results]))
    else:
        mark = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
        for s, c, d in results:
            print(f"{mark[s]}  {c}: {d}")
    return 1 if any(s == FAIL for s, _, _ in results) else 0


def exercise(arbiter, name: str, wait: float, json_out: bool) -> int:
    """Run one real cycle on a workload, under the lock, and time each step:
    take the GPU for it (releasing the others), load it when it can be loaded
    ahead of a request, then release it and confirm its VRAM came back. Ends
    with the GPU free. This is the test for a new driver: everything it would
    do in a real handover, in one go."""
    import time

    from .arbiter import NONE
    from .errors import SwitchError

    w = arbiter.workloads[name]
    steps = []

    def step(what: str, fn) -> bool:
        t = time.monotonic()
        try:
            detail = fn() or ""
        except SwitchError as e:
            steps.append((FAIL, what, f"{e}", time.monotonic() - t))
            return False
        steps.append((OK, what, detail, time.monotonic() - t))
        return True

    def vram():
        return arbiter.snapshot()[f"{name}_mib"]

    if (busy := w.busy()):
        steps.append((FAIL, "idle", busy, 0.0))
    else:
        lock = arbiter.lock.take(f"switch {name}", on_wait=lambda _h: util.log("waiting for another switch or hold"))
        with lock:
            ok = step("take the GPU", lambda: "; ".join(arbiter.acquire(name, wait, load=False)) or "others released")
            if ok and w.can_load():
                ok = step("load", lambda: (w.load(wait), f"{vram()} MiB of VRAM")[1])
                if ok:
                    s = arbiter.probe()
                    if not w.active(s[name], s["gpu"]):
                        steps.append((FAIL, "resident", f"loaded, but `resident` doesn't say so ({s[name]})", 0.0))
                        ok = False
                    else:
                        steps.append((OK, "resident", "`resident` says loaded", 0.0))
            elif ok:
                steps.append((OK, "load", "loads on its own first request: skipped", 0.0))
            if ok:
                before = vram()
                ok = step("release", lambda: "; ".join(w.release(wait, arbiter.release_timeout_s))
                          or f"{before} → {vram()} MiB")
            if ok:
                s = arbiter.probe()
                if w.active(s[name], s["gpu"]):
                    steps.append((FAIL, "released", f"still resident after release ({s[name]}, {vram()} MiB)", 0.0))
                else:
                    steps.append((OK, "released", f"{vram()} MiB left (idle: ≤ {w.idle_vram_mib})", 0.0))
        arbiter.events.record("exercise", workload=name, ok=all(s == OK for s, *_ in steps),
                              steps=[{"step": n, "ok": s == OK, "s": round(t, 1), "detail": d} for s, n, d, t in steps])
        if arbiter.probe()["mode"] not in (NONE, name):
            util.log("the GPU isn't free after the exercise; run `vhotseat status`")

    if json_out:
        print(json.dumps([{"status": s, "step": n, "detail": d, "seconds": round(t, 1)} for s, n, d, t in steps]))
    else:
        mark = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
        for s, n, d, t in steps:
            print(f"{mark[s]}  {name}: {n} ({t:.1f}s){': ' + d if d else ''}")
    return 1 if any(s == FAIL for s, *_ in steps) else 0
