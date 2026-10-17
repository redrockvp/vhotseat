"""ComfyUI: /queue, /free, /history; restarted when /free isn't enough.

Its gate is the guard custom node (comfyui/gpu_workload_guard), which takes the
GPU before every job. /free only takes effect between jobs, and empties VRAM
but not RAM: after a video render an idle ComfyUI kept 6-15 GiB of RAM + swap
(2026-10-16), so it's restarted when it holds more than `restart_ram_mib`.
"""

import time
from dataclasses import dataclass

from .. import util
from ..errors import Busy, ReleaseFailed, SwitchError
from .base import Workload


@dataclass
class ComfyUI(Workload):
    health_path: str = "/queue"
    # Its CUDA context alone is ~450 MiB; above this it is holding models.
    idle_vram_mib: int = 1500
    allow_restart: bool = True
    # RAM + swap (MiB) above which an idle ComfyUI is restarted after /free.
    restart_ram_mib: int = 4096
    # A restart wipes /history. comfy-render polls it every 2 s, so a job that
    # finished less than this long ago may not have been reported yet: wait first.
    history_settle_s: float = 10

    def _queue(self) -> tuple[int, int] | None:
        """(running, pending), or None if ComfyUI didn't answer."""
        code, body = util.http("GET", f"{self.url}/queue")
        if code != 200 or not isinstance(body, dict):
            return None
        return len(body.get("queue_running") or []), len(body.get("queue_pending") or [])

    def jobs(self) -> int | None:
        """Jobs running or queued, or None if that's unknown."""
        q = self._queue()
        return None if q is None else sum(q)

    def probe(self, gpu: dict) -> dict:
        st = {"service": self.service_active(), "reachable": False, "running": 0, "pending": 0}
        if st["service"] and (q := self._queue()) is not None:
            st.update(reachable=True, running=q[0], pending=q[1])
        st["holds_models"] = gpu[f"{self.name}_mib"] > self.idle_vram_mib
        return st

    def active(self, st: dict, gpu: dict) -> bool:
        return bool(st["holds_models"] or st["running"] or st["pending"])

    def describe(self, st: dict) -> str:
        if text := super().describe(st):
            return text
        jobs = f"{st['running']} running / {st['pending']} queued"
        return f"{'models loaded' if st['holds_models'] else 'no models loaded'}, {jobs}"

    def busy(self) -> str | None:
        jobs = self.jobs()
        if jobs is None:
            # Unknown counts as busy, unless it plainly holds nothing: a stopped
            # service, or one still starting up (its queue is empty after a start).
            if not self.service_active() or self.vram_mib() <= self.idle_vram_mib:
                return None
            return (f"ComfyUI holds {self.vram_mib()} MiB but its API isn't answering, "
                    "so whether it has jobs is unknown")
        return f"ComfyUI has {jobs} job(s) running or queued" if jobs else None

    def held_mib(self) -> int:
        """RAM plus swap (MiB), 0 if unknown."""
        m = self.ctx.host.memory(self.unit)
        return (m["ram_mib"] or 0) + (m["swap_mib"] or 0)

    def _free(self, wait: float, timeout: float) -> None:
        """Make ComfyUI drop its models. Fails at once if it has jobs, unless `wait`
        gives them that long to finish: a render is never interrupted."""
        if (jobs := self.jobs()) != 0:
            if not wait:
                raise Busy("ComfyUI has jobs running or queued (use --wait SECONDS to wait for them)" if jobs
                           else "ComfyUI's API isn't answering, so whether it has jobs is unknown")
            util.log("waiting for ComfyUI's queue to drain")
            util.wait_until(lambda: self.jobs() == 0, wait, "ComfyUI's queue to drain", poll=2.0, error=Busy)
        # /free only takes effect between jobs. If one is queued after the check above,
        # the free is postponed past it and the wait below times out: fail, not collide.
        code, body = util.http("POST", f"{self.url}/free", {"unload_models": True, "free_memory": True})
        if code != 200:
            raise SwitchError(f"ComfyUI /free failed: {code} {body}")
        util.wait_until(
            lambda: self.vram_mib() <= self.idle_vram_mib, timeout,
            f"ComfyUI to release VRAM (below {self.idle_vram_mib} MiB); was a new job queued?",
        )

    def release(self, wait: float, timeout: float) -> list[str]:
        """Free its models; ReleaseFailed when its VRAM won't drop with the queue
        empty (an extension that doesn't unload fully, e.g. IndexTTS without our
        patch), or when its RAM + swap stays above restart_ram_mib."""
        done = []
        # Nothing on the GPU and no jobs known (none, or its API isn't up yet): nothing to free.
        if (held := self.vram_mib()) > self.idle_vram_mib or self.jobs():
            try:
                self._free(wait, timeout)
                done.append(f"freed its {held} MiB of models")
            except SwitchError:
                # A job queued since the check postpones /free past it: never restart then.
                # Nor when whether it has jobs is unknown.
                if self.jobs() != 0:
                    raise
                stuck = self.vram_mib()
                raise ReleaseFailed(f"ComfyUI kept {stuck} MiB of VRAM after /free",
                                    why=f"it kept {stuck} MiB of VRAM after /free",
                                    then=f"restarted it: kept {stuck} MiB of VRAM after /free") from None
        if (ram := self.held_mib()) > self.restart_ram_mib:
            raise ReleaseFailed(f"ComfyUI holds {ram} MiB of RAM + swap",
                                why=f"it holds {ram} MiB of RAM + swap with no models loaded",
                                then=f"restarted it to release {ram} MiB of RAM + swap", done=done)
        return done

    def _last_finish(self) -> float | None:
        """When ComfyUI's newest /history entry finished (epoch seconds), or None."""
        code, body = util.http("GET", f"{self.url}/history?max_items=1")
        if code != 200 or not isinstance(body, dict) or not body:
            return None
        entry = list(body.values())[-1]
        stamps = [m[1].get("timestamp") for m in (entry.get("status") or {}).get("messages") or []
                  if isinstance(m, list) and len(m) == 2 and isinstance(m[1], dict)]
        stamps = [t for t in stamps if isinstance(t, (int, float))]
        return max(stamps) / 1000 if stamps else None

    def ready_to_restart(self) -> None:
        """A restart wipes /history, so wait until the newest job finished
        history_settle_s ago (comfy-render has read it by then), then re-check
        the queue right before stopping."""
        if (done := self._last_finish()) is not None:
            if (left := done + self.history_settle_s - time.time()) > 0:
                util.log(f"waiting {left:.0f}s so the last job's result is read before the restart")
                util.sleep(left)
        jobs = self.jobs()
        if jobs is None:
            raise SwitchError("ComfyUI isn't answering, so whether it has jobs is unknown; not restarting it")
        if jobs:
            raise Busy("a ComfyUI job was queued just now; not restarting it")
