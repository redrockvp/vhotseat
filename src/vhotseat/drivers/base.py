"""The contract between the arbiter and a workload driver."""

from dataclasses import dataclass, field
from typing import Callable

from .. import util
from ..errors import ReleaseFailed, SwitchError


@dataclass
class Context:
    """What drivers get from the arbiter: a fresh GPU snapshot, keyed "<name>_mib"."""

    gpu: Callable[[], dict]


@dataclass
class Workload:
    """One workload on the GPU. Subclasses implement probe/active/release, and
    load when the workload can be loaded ahead of its first request."""

    name: str                  # as used on the CLI and in the event log
    label: str                 # as shown to people
    url: str
    service: object            # host.Systemd or host.NoService
    ctx: Context
    # How its VRAM is found among the GPU's processes: ("unit", "x.service") or
    # ("process", "substring of its command line").
    vram_selector: tuple | None = None
    health_path: str = "/"
    # VRAM it may hold and still count as having released the GPU (ComfyUI keeps
    # its CUDA context and some caches).
    idle_vram_mib: int = 0
    # Ask it to release even when it doesn't look loaded: a load may be under way
    # before any VRAM shows (how-it-works §3.3).
    release_always: bool = False
    # Whether the arbiter may restart its service when releasing isn't enough.
    allow_restart: bool = False
    restart_timeout_s: float = 50
    # What must be available before it may load: min_available_ram_mib, min_free_vram_mib,
    # and by_model: {MODEL: {...}} overriding them for the model it's about to load.
    admission: dict = field(default_factory=dict)
    # How it asks for the GPU: {"kind": "hook" | "plugin" | "hold", "timeout": s, …}.
    gate: dict = field(default_factory=dict)

    # --- questions ---

    def service_active(self) -> bool:
        return self.service.is_active()

    def vram_mib(self) -> int:
        return self.ctx.gpu()[f"{self.name}_mib"]

    def memory(self) -> dict:
        return self.service.memory()

    def model(self) -> str | None:
        """The model it will load (or has loaded), if it can say; None otherwise."""
        return None

    def admission_for(self) -> tuple[dict, str | None]:
        """(the admission to check, the model it was picked for). A model listed
        under by_model gets its own figures; any other, or an unknown one, the
        defaults, so an unreadable model never means no check."""
        base = {k: v for k, v in self.admission.items() if k != "by_model"}
        per = self.admission.get("by_model") or {}
        if not per:
            return base, None
        model = self.model()
        return ({**base, **per[model]}, model) if model in per else (base, model)

    def held_mib(self) -> int:
        """RAM plus swap (MiB), 0 if unknown."""
        m = self.memory()
        return (m["ram_mib"] or 0) + (m["swap_mib"] or 0)

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
        unknown), or None when it's known to be idle, or when only release()
        can tell. Unknown counts as busy."""
        return None

    # --- actions ---

    def ensure_running(self, timeout: float = 60.0) -> None:
        """Start its service if it's stopped, and wait until its API answers."""
        if not self.service_active():
            util.log(f"starting {self.service.unit}")
            self.service.start()
        url = self.url + self.health_path
        util.wait_until(lambda: util.http("GET", url)[0] == 200, timeout, f"{self.label} to answer {url}")

    def release(self, wait: float, timeout: float) -> list[str]:
        """Drop its models without interrupting work, waiting up to `wait` for
        work to finish. Returns what was done, for the event log. Raises Busy,
        ReleaseFailed (restarting may help) or SwitchError."""
        raise NotImplementedError

    def can_load(self) -> bool:
        """Whether load() does anything (it can be loaded ahead of a request)."""
        return False

    def load(self, wait: float) -> None:
        """Load ahead of its first request. Lazy workloads leave this alone."""

    def ready_to_restart(self) -> None:
        """Raise SwitchError if restarting it now would lose something."""

    def restart(self, why: str) -> None:
        self.ready_to_restart()
        util.log(f"restarting {self.label}: {why}")
        self.service.restart()
        url = self.url + self.health_path
        util.wait_until(lambda: util.http("GET", url)[0] == 200, self.restart_timeout_s,
                        f"{self.label} to answer after its restart")


__all__ = ["Context", "Workload", "ReleaseFailed", "SwitchError"]
