"""`vhotseat doctor`: check that the guarantees actually hold on this machine.

Gates live outside vhotseat: a line in a server's config, a node in ComfyUI's
custom_nodes. If one is missing nothing fails; the GPU just stops being
guarded. So doctor checks every gate is installed and pointing here, as well
as the config, the GPU backend and each workload. Read-only: it never
switches, loads or unloads anything.
"""

import json
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
    guard = Path(nodes).expanduser() / "gpu_workload_guard"
    if not (guard / "__init__.py").exists():
        return [(FAIL, f"{w.name}: gate", f"guard node not installed in {nodes}: jobs aren't guarded")]
    return [(OK, f"{w.name}: gate", f"guard node installed ({guard.resolve()})")]


def checks(arbiter, cfg: dict | None) -> list:
    out = []
    g = arbiter.snapshot()
    if "error" in g:
        out.append((FAIL, "gpu", g["error"]))
    else:
        out.append((OK, "gpu", f"{arbiter.gpu.name}: {g['free_mib']} of {g['total_mib']} MiB free"))
    ram = arbiter.machine_ram()
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
        if (need := w.admission.get("min_free_vram_mib")) and g.get("total_mib") and need > g["total_mib"]:
            out.append((FAIL, f"{w.name}: admission", f"min_free_vram_mib {need} is more than the card has"))
        if (need := w.admission.get("min_available_ram_mib")) and ram.get("total_mib") and need > ram["total_mib"]:
            out.append((FAIL, f"{w.name}: admission", f"min_available_ram_mib {need} is more than the machine has"))
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
