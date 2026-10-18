---
name: vhotseat-comfyui-update
description: Check and repair vhotseat's ComfyUI guard after ComfyUI is updated. Use when the user updates ComfyUI (git pull, ComfyUI Manager, a new portable or Desktop version) on a machine with vhotseat, or when ComfyUI jobs start failing or running on top of the LLM after an update.
---

# After a ComfyUI update

The vhotseat guard node takes the GPU for each ComfyUI job before it runs. It
wraps a **private** ComfyUI method (`execution.PromptExecutor.execute_async`),
so an update can switch it off or break every job. This is the most likely
thing in a vhotseat setup to break.

The procedure, with the exact commands and what each check should print, is
`docs/comfyui-updates.md` in the vhotseat repo. Follow it; this is the order
and the rules.

## Rules

- **Find ComfyUI's folder and service first** (`gate.custom_nodes` in
  `~/.config/vhotseat/config.yaml` is inside it; `service:` names the unit).
  Use them wherever the doc says `$COMFY` and `comfyui`.
- **Note the commit before updating** (`git rev-parse --short HEAD`), so you can
  roll back. If the user already updated, find it in `git reflog`.
- **Ask before rolling back ComfyUI or editing the guard.** Show the diff first.
- Run the smoke tests while nobody else is using the GPU: they load and unload
  the LLM.
- **Don't report success without step 4.**

## 1. Guard on?

Restart ComfyUI and look for `vhotseat_guard: on` in its log. `OFF` means the
wrapped method is gone: go to step 5.

## 2. Signature unchanged?

Compare `async def execute_async` in ComfyUI's `execution.py` with what the
doc expects, and check `prompt_worker` in `main.py` still calls it once per
job. A changed signature means every job will fail with a `TypeError`: go to
step 5.

## 3. Run the smoke tests

The table in the doc, tests 1–5: a job takes the GPU from the LLM; a job waits
for an answer the LLM is writing; jobs and LLM requests alternate; a refused
job fails cleanly with `GPU busy, job not run`; a `comfy-render` batch reports
every job with its file. `integrations/comfyui/smoke_job.py --ckpt NAME`
queues one tiny job; pick a checkpoint the user has (`ls $COMFY/models/checkpoints`).
Restore `VHOTSEAT_GUARD_WAIT_S` after test 4.

## 4. Verify

1. `vhotseat doctor`: no FAIL.
2. All five smoke tests passed. Report each result to the user, with the
   ComfyUI commit you checked.

## 5. If something broke

Tell the user what broke and offer two ways out:

- **Roll back** to the noted commit and restart. Then repeat steps 1–4.
- **Fix the guard**, per *Fixing it* in the doc: wrap whatever the worker now
  calls once per job, on the worker thread, before models load. Never move it
  to a submit-time handler: that blocks ComfyUI's web server. Then repeat
  steps 1–4, and suggest contributing the fix to vhotseat.

Until it's fixed, ComfyUI can run unguarded with `VHOTSEAT_GUARD=0` in its
unit's environment, provided the user runs `vhotseat switch comfy` before
queueing work.
