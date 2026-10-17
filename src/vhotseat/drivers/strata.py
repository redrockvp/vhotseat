"""Strata (local LLM server): /health, /unload?wait=N, /load.

Strata's own `before_load` hook is its gate (`gpu-workload hook
strata-before-load`). It doesn't say whether it's answering, so "busy" is
learned from /unload: 409 or "busy" while a request or a load runs.
"""

from dataclasses import dataclass

from .. import util
from ..errors import Busy, SwitchError
from .base import Workload


@dataclass
class Strata(Workload):
    health_path: str = "/health"
    release_always: bool = True

    def probe(self, gpu: dict) -> dict:
        st = {"service": self.service_active(), "reachable": False, "loaded": False}
        if st["service"]:
            code, body = util.http("GET", f"{self.url}/health")
            if code == 200 and isinstance(body, dict):
                st.update(reachable=True, loaded=bool(body.get("loaded")))
        return st

    def active(self, st: dict, gpu: dict) -> bool:
        # nvidia-smi is the tiebreaker if /health is unreachable but the engine is on the GPU.
        return st["loaded"] or gpu[f"{self.name}_mib"] > 0

    def describe(self, st: dict) -> str:
        return super().describe(st) or ("model loaded" if st["loaded"] else "model unloaded (lazy)")

    def release(self, wait: float, timeout: float) -> list[str]:
        # Always asked, even when it looks unloaded: a load may be under way (its
        # engine not on the GPU yet), and /unload waits behind it. When it is already
        # unloaded this costs one request and answers "not loaded".
        # wait=N lets a running answer finish; without it Strata answers "busy" at once.
        url = f"{self.url}/unload" + (f"?wait={int(wait)}" if wait else "")
        code, body = util.http("POST", url, {}, timeout=wait + 30)
        status = body.get("status") if isinstance(body, dict) else body
        if code == 409 or status == "busy":
            raise Busy("Strata is answering or loading (use --wait SECONDS to let it finish)")
        if code != 200 or status not in ("unloaded", "not loaded"):
            raise SwitchError(f"Strata /unload failed: {code} {body}")
        util.wait_until(lambda: self.vram_mib() == 0, 30, "Strata's engine to leave the GPU")
        return []

    def load(self, wait: float) -> None:
        util.log("loading Strata's model")
        # Synchronous: answers once the engine is up. A cold load reads ~33 GB of experts.
        code, body = util.http("POST", f"{self.url}/load", {}, timeout=max(300.0, wait))
        if code != 200:
            raise SwitchError(f"Strata /load failed: {code} {body}")
