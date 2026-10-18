---
name: comfyui-batching
description: Use whenever you run ComfyUI workflows (anything that would POST to ComfyUI's /prompt) and the GPU is shared with the LLM you run on, through vhotseat. Run them with the `comfy-render` command, in as few large batches as possible.
---

# Running ComfyUI work on a shared GPU

You run on a **local LLM on the same GPU as ComfyUI**. They can't both be
loaded, so vhotseat hands the GPU over automatically:

- When a ComfyUI job starts, your model is unloaded (usually a second or two).
- Your next request after the jobs finish reloads it (often half a minute or
  more). Unloading also clears the LLM's cache, so it re-reads this whole
  conversation from the start.
- While ComfyUI has jobs queued or running, your LLM refuses to load (HTTP
  503). A render is never interrupted for you.

You don't manage any of this yourself. But two things matter.

## 1. Use `comfy-render`, not your own polling script

```sh
comfy-render jobs/<name>.json
```

Keep job files and other scratch files in your own working folder, never
inside ComfyUI's folder. Run it exactly like that:

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
queued, your next request to the LLM arrives while ComfyUI holds the GPU, and
the turn fails with "GPU busy".

**`--hold`**: `comfy-render --hold jobs/<name>.json` keeps the GPU on ComfyUI
for the whole batch, even between jobs. Use it when someone else may talk to
the same LLM while you render (the user in another chat, another agent): their
request is refused until the batch is done, instead of costing two handovers
in the middle of it.

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
- `job 2/4 ok: /path/a.png, /path/b.png`: the files that job saved.
- `job 3/4 rejected: node 7 PreviewImage: …`: ComfyUI refused the workflow
  before running it (a wrong link type, a missing input, an unknown node, a
  model file that isn't installed, which also lists the valid names). Fix that
  node and re-run **only** the jobs that failed. ComfyUI reports the first
  problem it meets, so a second error may appear after you fix the first.
- `job 1/4 failed: node 5 KSampler: …`: it ran but errored partway (e.g. out of
  memory). Add `--verbose` to see where in ComfyUI's code.
- `job 2/4 cancelled: cancelled by the user …`: someone stopped that job. It is
  not an error to fix. **Don't re-run it; ask the user why they cancelled** and
  go on from their answer. The exit code is 4 whenever any job was cancelled.
- `job 4/9 timeout: no progress for 1800s; …`: nothing happened for 30
  minutes, so ComfyUI is probably stuck. Those jobs are **still in ComfyUI's
  queue**. Tell the user rather than re-running them, since your next request
  may fail with "GPU busy" until the queue is cleared. The exit code is 3.
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
- **Keep scratch files out of ComfyUI's folders.** Contact sheets, extracted
  frames and other review material go in your working folder. ComfyUI's
  `input/` is only for files a workflow reads (e.g. with `LoadImage`); its
  `output/` is for deliverables. When an output feeds a later workflow,
  move it to `input/` rather than copying it.
- **Do non-GPU work outside the batch.** Layout, captions, file moves and
  contact sheets don't need the GPU. Do them before you queue the batch or after
  you've reviewed it.

One job at a time is fine when the next prompt really depends on the result
(tuning one parameter step by step), or when the user asked for exactly one
image. Say so in your reply if you expect several rounds.

## If you see "GPU busy"

- A **job** failing with `GPU busy, job not run: …` means your LLM was busy
  when ComfyUI tried to take the GPU, for instance because another conversation
  was using it. Re-run that job a little later.
- **Your own reply** failing because the GPU is busy with ComfyUI jobs means
  ComfyUI was still rendering when you were asked to continue. It clears when
  the queue empties.

## For whoever sets up the agent

The agent's harness must retry a 503 (honouring `Retry-After`) from the LLM
for at least as long as the tail of a batch can take, typically a few minutes.
With a short retry window (some harnesses give up after ~15 s), turns fail at
the end of renders even though nothing is wrong.
