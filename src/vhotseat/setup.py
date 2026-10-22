"""Build the arbiter from a config: the GPU backend, the workloads, the lock and
the event log."""

import os
import shutil
from pathlib import Path

from . import config as cfgmod
from . import claims, host
from .arbiter import Arbiter
from .drivers.base import Context
from .events import EventLog
from .gpu import open_backend
from .lock import GpuLock
from .telemetry import Telemetry


def migrate_state(new: Path, env=os.environ) -> None:
    """gpu-workload, vhotseat's predecessor, kept its lock and log in
    ~/.local/state/gpu-workload. Move them here and leave a symlink, so old paths
    still work."""
    old = new.parent / "gpu-workload"
    if new.exists() or not old.is_dir() or old.is_symlink():
        return
    # Moved as a whole (lock file included, so a holder's open lock stays the lock).
    shutil.move(str(old), str(new))
    old.symlink_to(new.name)


def build(cfg: dict, *, env=os.environ, gpu=None, machine_ram=None, **arbiter_args) -> Arbiter:
    state = cfgmod.state_dir(env)
    migrate_state(state, env)
    g = cfg.get("gpu") or {}
    index = g.get("index", 0)
    tel = cfg.get("telemetry") or {}
    telemetry = None if tel.get("enabled", True) is False else Telemetry(
        state / "telemetry.db", retention_hours=tel.get("retention_hours", 24), interval_s=tel.get("interval_s", 5),
        seed_events=state / "events.jsonl")
    arbiter = None
    ctx = Context(gpu=lambda: arbiter.snapshot())
    workloads = cfgmod.workloads(cfg, ctx)
    arbiter = Arbiter(
        workloads, gpu or open_backend(g.get("backend", "auto"), index),
        # GPU 0 keeps gpu-workload's lock name, so a rollback to gpu-workload
        # shares the lock with anything still on vhotseat.
        GpuLock(state / ("switch.lock" if index == 0 else f"gpu{index}.lock")),
        EventLog(state / "events.jsonl", keep=(cfg.get("events") or {}).get("keep", 500), telemetry=telemetry),
        machine_ram=machine_ram or host.machine_ram, telemetry=telemetry, power=cfg.get("power") or {},
        claim_rules=claims.rules(cfg.get("claims") or [], cfg.get("claims_builtin", True)),
        claim_ignore=cfg.get("claims_ignore") or [],
        claim_poll_s=cfg.get("claims_poll_s", 2.0),
        pressure_cfg=cfg.get("pressure"),
        **arbiter_args,
    )
    return arbiter
