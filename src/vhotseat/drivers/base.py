"""The contract between the arbiter and a workload driver."""

from dataclasses import dataclass
from typing import Callable

from .. import util
from ..errors import ReleaseFailed, SwitchError


@dataclass
class Context:
    """What drivers get from the arbiter: the GPU and the service backend."""

    gpu: Callable[[], dict]  # a fresh GpuBackend snapshot, keyed "<name>_mib"
    host: object             # a service backend (host.SystemdUser)


@dataclass
class Workload:
    """One workload on the GPU. Subclasses implement probe/active/release, and
    load when the workload can be loaded ahead of its first request."""

    name: str                  # as used on the CLI and in the event log
    label: str                 # as shown to people
    url: str
    unit: str                  # its service
    proc: str                  # substring of its processes' paths in nvidia-smi
    ctx: Context
    health_path: str = "/"
    # VRAM a workload may hold and still count as having released the GPU
    # (ComfyUI keeps its CUDA context and some caches).
    idle_vram_mib: int = 0
    # Ask it to release even when it doesn't look loaded: a load may be under way
    # before any VRAM shows (how-it-works §3.3).
    release_always: bool = False
    # Whether the arbiter may restart its service when releasing isn't enough.
    allow_restart: bool = False
    restart_timeout_s: float = 50

    # --- questions ---

    def service_active(self) -> bool:
        return self.ctx.host.is_active(self.unit)

    def vram_mib(self) -> int:
        return self.ctx.gpu()[f"{self.name}_mib"]

    def probe(self, gpu: dict) -> dict:
        """This workload's part of `status`."""
        raise NotImplementedError

    def active(self, st: dict, gpu: dict) -> bool:
        """Whether it holds the GPU, judged from probe()'s answer."""
        raise NotImplementedError

    def describe(self, st: dict) -> str:
        """probe()'s answer as one line of `status`."""
        if not st["service"]:
            return "service stopped"
        if not st["reachable"]:
            return "service up, API not answering"
        return ""

    def busy(self) -> str | None:
        """Why it mustn't be released right now (work running, or its state is
        unknown), or None when it's known to be idle. Unknown counts as busy."""
        return None

    # --- actions ---

    def ensure_running(self, timeout: float = 60.0) -> None:
        """Start its service if it's stopped, and wait until its API answers."""
        if not self.service_active():
            util.log(f"starting {self.unit}.service")
            self.ctx.host.start(self.unit)
        url = self.url + self.health_path
        util.wait_until(lambda: util.http("GET", url)[0] == 200, timeout, f"{self.unit} to answer {url}")

    def release(self, wait: float, timeout: float) -> list[str]:
        """Drop its models without interrupting work, waiting up to `wait` for
        work to finish. Returns what was done, for the event log. Raises Busy,
        ReleaseFailed (restarting may help) or SwitchError."""
        raise NotImplementedError

    def load(self, wait: float) -> None:
        """Load ahead of its first request. Lazy workloads leave this alone."""

    def ready_to_restart(self) -> None:
        """Raise SwitchError if restarting it now would lose something."""

    def restart(self, why: str) -> None:
        self.ready_to_restart()
        util.log(f"restarting {self.label}: {why}")
        self.ctx.host.restart(self.unit)
        url = self.url + self.health_path
        util.wait_until(lambda: util.http("GET", url)[0] == 200, self.restart_timeout_s,
                        f"{self.unit} to answer after its restart")


__all__ = ["Context", "Workload", "ReleaseFailed", "SwitchError"]
