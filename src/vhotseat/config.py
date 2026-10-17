"""config.yaml: which workloads share the GPU, and how to talk to each.

Found at $VHOTSEAT_CONFIG, else $XDG_CONFIG_HOME/vhotseat/config.yaml
(~/.config/vhotseat/config.yaml). See config.example.yaml for every key.
Validated when loaded: a mistake is reported with its key path, and nothing runs.
"""

import os
from pathlib import Path

from .drivers.base import Context, Workload
from .drivers.comfyui import ComfyUI
from .drivers.http import HttpWorkload
from .host import service


class ConfigError(Exception):
    pass


def default_path(env=os.environ) -> Path:
    if p := env.get("VHOTSEAT_CONFIG"):
        return Path(p).expanduser()
    return Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "vhotseat" / "config.yaml"


def state_dir(env=os.environ) -> Path:
    return Path(env.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "vhotseat"


# --- validation ----------------------------------------------------------------

GATE_KINDS = ("hook", "plugin", "hold")
SERVICE_KINDS = ("systemd-user", "systemd")
COMMON = {"driver", "label", "url", "service", "vram", "idle_vram_mib", "gate", "admission",
          "allow_restart", "restart_timeout_s"}
GATE_KEYS = {"kind", "timeout", "config", "custom_nodes"}
DRIVER_KEYS = {
    "http": {"resident", "busy", "release", "confirm", "load"},
    "comfyui": {"restart_above_ram_mib", "history_settle_s"},
}


def _check(cond, where: str, msg: str):
    if not cond:
        raise ConfigError(f"{where}: {msg}")


def _int(d: dict, key: str, where: str, minimum: int = 0):
    if key in d:
        _check(isinstance(d[key], int) and not isinstance(d[key], bool) and d[key] >= minimum,
               f"{where}.{key}", f"expected a whole number ≥ {minimum}, got {d[key]!r}")


def _request(d, where: str, needs_path=False):
    _check(isinstance(d, dict), where, "expected a mapping with `get:` or `post:`")
    _check(("get" in d) != ("post" in d), where, "needs exactly one of `get:` or `post:`")
    url = d.get("get") or d.get("post")
    _check(isinstance(url, str) and url.startswith("/"), where, f"the path must start with /, got {url!r}")
    if needs_path:
        _check(isinstance(d.get("path"), str), where, "needs `path:`, the JSON field to read")


def _rules(rules, where: str):
    _check(isinstance(rules, list), where, "expected a list of rules like {status: 409} or {path: x, in: [...]}")
    for i, r in enumerate(rules):
        _check(isinstance(r, dict) and ({"status", "path"} & set(r)), f"{where}[{i}]",
               "a rule needs `status:` and/or `path:`")


def validate(raw) -> dict:
    _check(isinstance(raw, dict), "config", "expected a mapping at the top level")
    unknown = set(raw) - {"gpu", "workloads", "events"}
    _check(not unknown, "config", f"unknown key(s): {', '.join(sorted(unknown))}")
    gpu = raw.get("gpu") or {}
    _check(isinstance(gpu, dict), "gpu", "expected a mapping")
    _check(gpu.get("backend", "auto") in ("auto", "nvml", "nvidia-smi"), "gpu.backend",
           "expected auto, nvml or nvidia-smi")
    _int(gpu, "index", "gpu")
    events = raw.get("events") or {}
    _check(isinstance(events, dict), "events", "expected a mapping")
    _int(events, "keep", "events", 10)

    wls = raw.get("workloads")
    _check(isinstance(wls, dict) and wls, "workloads", "expected at least one workload")
    for name, w in wls.items():
        where = f"workloads.{name}"
        _check(isinstance(name, str) and name.replace("-", "").replace("_", "").isalnum() and name != "none",
               where, "a workload name must be letters, digits, - or _ (and not 'none')")
        _check(isinstance(w, dict), where, "expected a mapping")
        driver = w.get("driver")
        _check(driver in DRIVER_KEYS, f"{where}.driver", f"expected one of {', '.join(DRIVER_KEYS)}, got {driver!r}")
        unknown = set(w) - COMMON - DRIVER_KEYS[driver]
        _check(not unknown, where, f"unknown key(s) for driver {driver}: {', '.join(sorted(unknown))}")
        _check(isinstance(w.get("url"), str) and w["url"].startswith(("http://", "https://")),
               f"{where}.url", "expected an http:// URL")
        svc = w.get("service")
        if svc is not None:
            _check(isinstance(svc, dict) and len(svc) == 1 and next(iter(svc)) in SERVICE_KINDS,
                   f"{where}.service", f"expected {{systemd-user: UNIT}} or {{systemd: UNIT}}")
        vram = w.get("vram")
        if vram is not None:
            _check(isinstance(vram, dict) and len(vram) == 1 and next(iter(vram)) in ("unit", "process"),
                   f"{where}.vram", "expected {unit: NAME.service} or {process: SUBSTRING}")
        _check(vram is not None or svc is not None, f"{where}.vram",
               "needed when there's no service: how to find its processes on the GPU")
        _int(w, "idle_vram_mib", where)
        _int(w, "restart_timeout_s", where, 1)
        _check(isinstance(w.get("allow_restart", False), bool), f"{where}.allow_restart", "expected true or false")
        _check(not w.get("allow_restart") or svc is not None, f"{where}.allow_restart",
               "needs a `service:` to restart")
        adm = w.get("admission") or {}
        _check(isinstance(adm, dict), f"{where}.admission", "expected a mapping")
        unknown = set(adm) - {"min_available_ram_mib", "min_free_vram_mib"}
        _check(not unknown, f"{where}.admission", f"unknown key(s): {', '.join(sorted(unknown))}")
        for k in adm:
            _int(adm, k, f"{where}.admission")
        gate = w.get("gate")
        _check(isinstance(gate, dict) and gate.get("kind") in GATE_KINDS, f"{where}.gate",
               f"expected {{kind: {' | '.join(GATE_KINDS)}}}: how this workload asks for the GPU")
        _int(gate, "timeout", f"{where}.gate", 1)
        unknown = set(gate) - GATE_KEYS
        _check(not unknown, f"{where}.gate", f"unknown key(s): {', '.join(sorted(unknown))}")
        if driver == "http":
            _request(w.get("resident"), f"{where}.resident", needs_path=True)
            if "busy" in w:
                _request(w["busy"], f"{where}.busy", needs_path=True)
            rel = w.get("release")
            _request(rel, f"{where}.release")
            for k in ("busy_when", "ok_when"):
                if k in rel:
                    _rules(rel[k], f"{where}.release.{k}")
            if "load" in w:
                _request(w["load"], f"{where}.load")
            conf = w.get("confirm") or {}
            _check(isinstance(conf, dict), f"{where}.confirm", "expected a mapping")
            _int(conf, "vram_at_most", f"{where}.confirm")
        if driver == "comfyui":
            _int(w, "restart_above_ram_mib", where)
    return raw


def parse(text: str) -> dict:
    import yaml
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"not valid YAML: {e}") from None
    return validate(raw)


def load(path: Path) -> dict:
    try:
        return parse(path.read_text())
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}") from None
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None


# --- building -------------------------------------------------------------------


def workloads(cfg: dict, ctx: Context) -> list[Workload]:
    out = []
    for name, w in cfg["workloads"].items():
        svc_kind, unit = next(iter(w["service"].items())) if w.get("service") else (None, None)
        svc = service(svc_kind, unit)
        if w.get("vram"):
            selector = next(iter(w["vram"].items()))
        else:
            selector = ("unit", svc.unit)
        common = dict(
            name=name, label=w.get("label") or name, url=w["url"].rstrip("/"), service=svc, ctx=ctx,
            vram_selector=selector, gate=w["gate"], admission=w.get("admission") or {},
            allow_restart=w.get("allow_restart", False),
        )
        for k in ("idle_vram_mib", "restart_timeout_s"):
            if k in w:
                common[k] = w[k]
        if w["driver"] == "comfyui":
            extra = {k: w[k] for k in ("restart_above_ram_mib", "history_settle_s") if k in w}
            out.append(ComfyUI(**common, **extra))
        else:
            out.append(HttpWorkload(**common, spec={k: w[k] for k in DRIVER_KEYS["http"] if k in w}))
    return out
