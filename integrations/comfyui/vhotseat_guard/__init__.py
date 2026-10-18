"""vhotseat_guard: make every ComfyUI job take the GPU from Strata first.

ComfyUI and Strata (local LLM) can't hold VRAM at the same time. Strata's
side is guarded by its before_load hook (`vhotseat gate strata`); this is the
ComfyUI side. Before each job runs, it calls

    vhotseat switch comfy --wait VHOTSEAT_GUARD_WAIT_S

which lets an answer Strata is writing finish, then unloads Strata. If that
fails, the job fails visibly: an `execution_error` in the UI and history,
with vhotseat's reason. It never runs on top of a loaded Strata.

How: it wraps `execution.PromptExecutor.execute_async`, the method ComfyUI's
prompt worker thread calls once per job. That thread may block, so waiting
here doesn't freeze the web server. This is NOT a public ComfyUI API; see
docs/comfyui-updates.md for what to check after a ComfyUI update. If the
method is missing, the guard logs an error and stays off rather than
breaking ComfyUI.

Env:
  VHOTSEAT_BIN            default: vhotseat on PATH, else ~/.local/bin/vhotseat
  VHOTSEAT_GUARD_WAIT_S   how long to wait for Strata to finish an answer (default 300)
  VHOTSEAT_GUARD          set to 0 to disable

Until 2026-10-17 this was gpu_workload_guard, with GPU_WORKLOAD_* variables.
"""

import functools
import logging
import os
import shutil
import subprocess

log = logging.getLogger("vhotseat_guard")

NODE_CLASS_MAPPINGS: dict = {}  # no nodes; ComfyUI requires the attribute to load the package
NODE_DISPLAY_NAME_MAPPINGS: dict = {}

WAIT_S = int(os.environ.get("VHOTSEAT_GUARD_WAIT_S", "300"))
BIN = os.environ.get("VHOTSEAT_BIN") or shutil.which("vhotseat") or os.path.expanduser(
    "~/.local/bin/vhotseat"
)
MARKER = "_vhotseat_guard"


class GpuBusy(RuntimeError):
    pass


def take_gpu() -> None:
    """Switch the GPU to ComfyUI, or raise GpuBusy with vhotseat's reason."""
    try:
        r = subprocess.run(
            [BIN, "switch", "comfy", "--wait", str(WAIT_S)],
            capture_output=True, text=True, timeout=WAIT_S + 120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GpuBusy(f"vhotseat switch comfy could not run: {e}") from e
    if r.returncode != 0:
        lines = [ln.removeprefix("vhotseat: ") for ln in r.stderr.splitlines() if ln.strip()]
        raise GpuBusy(lines[-1] if lines else f"vhotseat switch comfy exited {r.returncode}")


def _fail_job(executor, prompt_id: str, prompt: dict, reason: str) -> None:
    """Report a job as failed the way ComfyUI does for a node error, so the
    frontend and /history show the reason, and the worker records status=error."""
    node_id = next(iter(prompt), "")
    executor.success = False
    executor.status_messages = []
    executor.history_result = {"outputs": {}, "meta": {}}
    executor.add_message("execution_start", {"prompt_id": prompt_id}, broadcast=False)
    executor.add_message("execution_error", {
        "prompt_id": prompt_id,
        "node_id": node_id,
        "node_type": prompt.get(node_id, {}).get("class_type", ""),
        "executed": [],
        "exception_message": f"GPU busy, job not run: {reason}",
        "exception_type": "vhotseat_guard.GpuBusy",
        "traceback": [],
        "current_inputs": {},
        "current_outputs": [],
    }, broadcast=False)


def install() -> bool:
    if os.environ.get("VHOTSEAT_GUARD", "1") == "0":
        log.warning("vhotseat_guard: disabled by VHOTSEAT_GUARD=0")
        return False
    try:
        import execution
        original = execution.PromptExecutor.execute_async
    except (ImportError, AttributeError) as e:
        log.error("vhotseat_guard: OFF, ComfyUI internals changed (%s); "
                  "jobs are NOT guarded. See vhotseat's docs/comfyui-updates.md", e)
        return False
    if getattr(original, MARKER, False) or getattr(original, "_gpu_workload_guard", False):
        # Already wrapped (this module loaded twice, or the old gpu_workload_guard still
        # installed next to it): wrapping again would switch the GPU twice per job.
        return True

    @functools.wraps(original)
    async def guarded(self, prompt, prompt_id, extra_data={}, execute_outputs=[]):  # noqa: B006 (mirrors original)
        try:
            take_gpu()
        except GpuBusy as e:
            log.warning("vhotseat_guard: job %s not run: %s", prompt_id, e)
            # execute_async normally sets this; add_message uses it to reach the submitting tab.
            self.server.client_id = extra_data.get("client_id")
            _fail_job(self, prompt_id, prompt, str(e))
            return
        return await original(self, prompt, prompt_id, extra_data, execute_outputs)

    setattr(guarded, MARKER, True)
    execution.PromptExecutor.execute_async = guarded
    log.info("vhotseat_guard: on (%s, wait %ss)", BIN, WAIT_S)
    return True


install()
