# vhotseat guard

A ComfyUI custom node with no nodes. Before each job runs, it calls

    vhotseat switch comfy --wait $VHOTSEAT_GUARD_WAIT_S

so the GPU is handed to ComfyUI first: a local LLM sharing the GPU finishes the
answer it is writing and is unloaded. If that fails, the job is not run; it
fails visibly with "GPU busy, job not run: <reason>".

It needs [vhotseat](https://github.com/wandermyz/vhotseat) installed and
configured (`vhotseat doctor` should pass). Install:

    comfy node install vhotseat-guard

or symlink this folder into `ComfyUI/custom_nodes/`. Restart ComfyUI after
installing or updating it.

Env: `VHOTSEAT_GUARD=0` disables it; `VHOTSEAT_GUARD_WAIT_S` (default 300) is
how long to wait for the LLM; `VHOTSEAT_BIN` points at the CLI.

It wraps a private ComfyUI method (`PromptExecutor.execute_async`), so a
ComfyUI update can break it. The guard then switches itself off with an error
in ComfyUI's log rather than breaking ComfyUI. See
[docs/comfyui-updates.md](https://github.com/wandermyz/vhotseat/blob/main/docs/comfyui-updates.md).
