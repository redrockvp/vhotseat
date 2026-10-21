"""vhotseat_guard: make every ComfyUI job take the GPU through vhotseat first.

ComfyUI and the other workloads vhotseat manages (a local LLM, say) can't hold
VRAM at the same time. Each of them asks vhotseat before it loads; this is the
ComfyUI side. Before each job runs, it calls

    vhotseat switch comfy --wait VHOTSEAT_GUARD_WAIT_S

which lets the other workloads finish what they're running (an answer the LLM
is writing), then releases them. If that fails, the job fails visibly: an
`execution_error` in the UI and history, with vhotseat's reason. It never runs
on top of another loaded workload.

How: it wraps `execution.PromptExecutor.execute_async`, the method ComfyUI's
prompt worker thread calls once per job. That thread may block, so waiting
here doesn't freeze the web server. This is NOT a public ComfyUI API; see
docs/comfyui-updates.md for what to check after a ComfyUI update. If the
method is missing, the guard logs an error and stays off rather than
breaking ComfyUI.

Env:
  VHOTSEAT_BIN            default: vhotseat on PATH, else ~/.local/bin/vhotseat (.exe on Windows)
  VHOTSEAT_GUARD_WAIT_S   how long to wait for running work elsewhere to finish (default 300)
  VHOTSEAT_GUARD          set to 0 to disable
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
    "~/.local/bin/vhotseat.exe" if os.name == "nt" else "~/.local/bin/vhotseat"
)
# Windows: no console window flashing up per job when ComfyUI runs without one.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
MARKER = "_vhotseat_guard"
# Set False the first time the installed vhotseat says it doesn't know --request, so every
# later job doesn't pay for a failed run.
REQUEST_OK = True


class GpuBusy(RuntimeError):
    pass


def take_gpu() -> None:
    """Switch the GPU to ComfyUI, or raise GpuBusy with vhotseat's reason."""
    global REQUEST_OK
    for attempt in (1, 2):
        # --request: the queue is asking on somebody's behalf, not a person typing
        # `vhotseat switch comfy`. A claimed process (a game) refuses this one, so a
        # job never loads a model on top of the card someone is playing on; it doesn't
        # refuse the typed command, and it doesn't refuse a job at all once ComfyUI
        # already holds the card, so a batch in flight carries on.
        cmd = [BIN, "switch", "comfy", "--request", "--wait", str(WAIT_S)] if REQUEST_OK \
            else [BIN, "switch", "comfy", "--wait", str(WAIT_S)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=WAIT_S + 120, **NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GpuBusy(f"vhotseat switch comfy could not run: {e}") from e
        if r.returncode == 0:
            return
        if REQUEST_OK and attempt == 1 and "--request" in r.stderr and "unrecognized" in r.stderr:
            # An installed vhotseat from before claims rejects the flag outright, which would
            # fail every job. Fall back to the typed switch: it's the weaker one - a claim
            # won't refuse it - but it's what this install can do, and the node still gates.
            REQUEST_OK = False
            log.warning("vhotseat at %s is older than claims: switching without --request, "
                        "so a running game won't refuse the load. Update vhotseat.", BIN)
            continue
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
