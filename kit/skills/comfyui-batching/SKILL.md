---
name: comfyui-batching
description: Use whenever you run ComfyUI workflows on this machine (anything that would POST to 127.0.0.1:8188/prompt). Run them with the `comfy-render` command, in as few large batches as possible, because the GPU is shared with the LLM you run on.
---

# Running ComfyUI work

You run on **Strata**, a local LLM, and ComfyUI renders on the **same 16 GB
GPU**. They can't both be loaded, so the GPU is handed over automatically:

- When a ComfyUI job starts, your model is unloaded (about 1 s).
- Your next request after the jobs finish reloads it (about 30 s). Because
  unloading clears the cache, it also re-reads this whole conversation from
  the start, at roughly 1 minute per 30K tokens.

You don't manage any of this yourself. But two things matter.

## 1. Use `comfy-render`, not your own polling script

```sh
comfy-render jobs/<name>.json
```

Write jobs files to the workspace's `jobs/` folder, never into the ComfyUI
folder. Run it exactly like that:

- **Don't pipe its output** (no `| tail`, `| head`, `| grep`). Its progress
  lines are how the user follows a long render live; a pipe holds them back
  until the end.
- **Don't set a timeout** on the shell command. A batch of videos can take
  hours, and comfy-render already gives up on its own if nothing progresses
  for 30 minutes. If you cut it off while jobs are still queued, ComfyUI keeps
  the GPU and your next request fails with "GPU busy".

It queues every job, **waits until all of them have finished**, and prints one
line per job: the files it saved, or ComfyUI's own error. A hand-written
polling loop is easy to get subtly wrong. If it crashes while jobs are still
queued, your next request to Strata arrives while ComfyUI holds the GPU, and
the turn fails with "GPU busy".

You still design the workflows yourself: any nodes, models, LoRAs, prompts,
seeds and sizes. Write them as ComfyUI **API-format** JSON, the same graph you
would POST to `/prompt`, in one of three shapes:

```jsonc
// one workflow
{"1": {"class_type": "CheckpointLoaderSimple", "inputs": {...}}, "2": {...}}

// several different workflows
[ {workflow A}, {workflow B} ]

// one base workflow plus variants: each overrides NODE.inputs.NAME
{"base": {workflow},
 "jobs": [{"5.inputs.seed": 11}, {"5.inputs.seed": 22}, {"2.inputs.text": "…"}]}
```

Use a `Save*` output node (e.g. `SaveImage`) for files you want to keep;
`PreviewImage` writes to ComfyUI's temp folder.

Reading the result:

- While it runs you'll see progress such as `job 1/2 running: node 5 KSampler
  step 57/150 (38%)` or `job 1/2 waiting to start …`. Just let it finish; don't
  start other ComfyUI work in the meantime.
- `job 2/4 ok: /path/a.png, /path/b.png` — the files that job saved.
- `job 3/4 rejected: node 7 PreviewImage: …` — ComfyUI refused the workflow
  before running it (a wrong link type, a missing input, an unknown node, a
  model file that isn't installed, which also lists the valid names). Fix that
  node and re-run **only** the jobs that failed. ComfyUI reports the first
  problem it meets, so a second error may appear after you fix the first.
- `job 1/4 failed: node 5 KSampler: …` — it ran but errored partway (e.g. out of
  memory). Add `--verbose` to see where in ComfyUI's code.
- `job 2/4 cancelled: cancelled by the user …` — the user stopped that job
  (from the dashboard or ComfyUI). It is not an error to fix. **Don't re-run
  it; ask the user why they cancelled** and go on from their answer. The exit
  code is 4 whenever any job was cancelled.
- `job 4/9 timeout: no progress for 1800s; …` — nothing happened for 30
  minutes, so ComfyUI is probably stuck. Those jobs are **still in ComfyUI's
  queue**. Tell the user rather than re-running them, since your next
  request may fail with "GPU busy" until the queue is cleared. The exit code
  is 3.
- The last line is the total; the exit code is 0 only if every job saved files.

Writing your own script is still fine for something `comfy-render` can't do.
If you do, wait until the queue is empty (`GET /queue`) before finishing, even
if your script hits an error.

## 2. Batch, as a best effort

Every round of "render, look, render again" pays the GPU handover twice. Batch
when it's natural; don't bend the task out of shape for it.

- **Queue everything for a round in one `comfy-render` call.** Write every
  prompt for the whole job (all panels, all shots) before rendering anything.
- **Over-generate instead of re-rolling.** If a panel or shot is risky, render
  more seeds (e.g. 6–8 instead of 3) and a few prompt variants in the same
  batch. Picking from extras is cheaper than a second round.
- **Put review material in one image.** After a batch, build one contact sheet
  per round (a labelled grid of thumbnails) and look at that, instead of opening
  every output.
- **Scratch files go to the workspace's `intermediate/` folder.** Contact
  sheets, extracted check frames and other review material that no workflow
  loads belong there. ComfyUI's `input/` is only for files a workflow reads
  (e.g. with `LoadImage`); its `output/` is only for deliverables. When an
  output image feeds a later workflow (reference, init or start image),
  move it from `output/` to `input/` with `mv`; don't copy it.
- **Do non-GPU work outside the batch.** Layout, captions, file moves and
  contact sheets don't need the GPU. Do them before you queue the batch or after
  you've reviewed it.

One job at a time is fine when the next prompt really depends on the result
(tuning one parameter step by step), or when the user asked for exactly one
image. Say so in your reply if you expect several rounds.

## If you see "GPU busy"

- A **job** failing with `GPU busy, job not run: …` means your model was busy
  when ComfyUI tried to take the GPU, for instance because another conversation
  was using it. Re-run that job a little later.
- **Your own reply** failing with "the GPU is busy with ComfyUI jobs" means
  ComfyUI was still rendering when you were asked to continue. It clears when
  the queue empties.
