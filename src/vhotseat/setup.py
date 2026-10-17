"""Build the arbiter from a config: the GPU backend, the workloads, the lock and
the event log. Shared by the `vhotseat` and `gpu-workload` CLIs, so both take
the same lock and write the same log."""

import os
import shutil
from pathlib import Path

from . import config as cfgmod
from . import host
from .arbiter import Arbiter
from .drivers.base import Context
from .events import EventLog
from .gpu import open_backend
from .lock import GpuLock


def migrate_state(new: Path, env=os.environ) -> None:
    """gpu-workload kept its lock and log in ~/.local/state/gpu-workload. Move the
    log here and leave a symlink, so old paths in docs and notes still work."""
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
    arbiter = None
    ctx = Context(gpu=lambda: arbiter.snapshot())
    workloads = cfgmod.workloads(cfg, ctx)
    arbiter = Arbiter(
        workloads, gpu or open_backend(g.get("backend", "auto"), index),
        # GPU 0 keeps gpu-workload's lock name, so a gpu-workload from before the
        # config (a rollback, or a hook still pointing at an old copy) takes the
        # same lock: two lock files would let both hand the GPU over at once.
        GpuLock(state / ("switch.lock" if index == 0 else f"gpu{index}.lock")),
        EventLog(state / "events.jsonl", keep=(cfg.get("events") or {}).get("keep", 500)),
        machine_ram=machine_ram or host.machine_ram, **arbiter_args,
    )
    return arbiter
