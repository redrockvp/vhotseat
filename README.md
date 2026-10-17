# gpu-workload

Hands the single 16 GB GPU between **Strata** (local LLM, `strata.service`,
`:2335`) and **ComfyUI** (`comfyui.service`, `:8188`). They can't both hold VRAM.

**New here?** Read [docs/how-it-works.md](docs/how-it-works.md) first. It
walks through a full the chat bot → pi → ComfyUI → pi flow as a sequence
diagram, then covers the race conditions and deadlocks, and how often the GPU
changes hands. This README is the reference.

```
gpu-workload status [--json]
gpu-workload switch strata|comfy [--wait SECONDS] [--json]
gpu-workload check  strata|comfy [--json]
gpu-workload hold comfy [--wait SECONDS] -- COMMAND...
gpu-workload hook strata-before-load     # called by Strata, not by hand
```

Exit codes: `0` ok · `1` switch failed or hook refused · `2` usage · `3` `check` found the wrong mode.
`hold` exits with COMMAND's own exit code.

## Modes

The mode is **derived live** from Strata `/health`, ComfyUI `/queue` and
`nvidia-smi`. There is no state file, so it can't drift.

| Mode | Meaning |
| --- | --- |
| `strata` | Strata's model is loaded; ComfyUI holds no models and has no jobs |
| `comfy` | Strata's model is unloaded (ComfyUI may or may not hold models) |
| `conflict` | both at once; `switch` either way resolves it |

## Switching

Both directions are idempotent: switching to the current mode only re-checks it.

| Command | What it does |
| --- | --- |
| `switch strata` | Waits for ComfyUI's queue to drain (fails at once unless `--wait N`), `POST /free`, waits until ComfyUI's VRAM drops below 1500 MiB, [restarts ComfyUI if it still holds a lot of RAM](#comfyui-restart-ram-not-just-vram), then `POST /load` on Strata, which blocks until the model is up |
| `switch comfy` | `POST /unload` on Strata (`?wait=N` with `--wait`, to let a running answer finish), waits until its engine leaves the GPU |

- If a service is stopped, `switch` starts it (`systemctl --user start`).
- Concurrent switches are serialized with a lock in
  `~/.local/state/gpu-workload/`.
- `--wait` matters because ComfyUI's `/free` only acts *between* jobs, and
  Strata's `/unload` refuses while it's answering.

Measured 2026-10-12 (RTX 5070 Ti, Strata IQ2_XS):

| Switch | Time |
| --- | --- |
| to strata, cold (experts read from disk) | 68 s |
| to strata, warm (OS page cache) | 26 s |
| to comfy | ~1 s |

`switch comfy` always asks Strata to unload when its service is running, even
if it looks unloaded. A load in progress hasn't reached the GPU yet, and
Strata's `/unload` waits behind it, so the switch can't report `comfy` while
Strata is still coming up.

## Each side takes the GPU for itself

| Wants the GPU | Who makes room | How |
| --- | --- | --- |
| Strata (any caller: pi, the chat bot, curl) | Strata's `before_load` hook | frees an idle ComfyUI; refuses while a job is queued or running |
| ComfyUI (any caller: a script, its web page) | the ComfyUI guard node | before each job, `switch comfy --wait 300`: lets an answer Strata is writing finish, then unloads it |

Callers need no GPU logic: pi, scripts and the ComfyUI web page just use
their server. A Strata request between two ComfyUI jobs works, but costs
about 30 s per handover (Strata reload, then ComfyUI reloads its model).

## ComfyUI guard node

`comfyui/gpu_workload_guard` is installed as a symlink:
`~/ComfyUI/custom_nodes/gpu_workload_guard`. ComfyUI must be restarted
after installing or changing it.

- **How it hooks in:** it wraps `execution.PromptExecutor.execute_async`, which
  ComfyUI's prompt worker thread calls once per job, and runs
  `gpu-workload switch comfy --wait $GPU_WORKLOAD_GUARD_WAIT_S` (default 300)
  first. The worker thread may block, so the web server stays responsive.
- **On failure:** the job is **not run** and fails visibly, with an
  `execution_error` "GPU busy, job not run: <reason>" in the UI and in
  `/history`.
- **It's a private API:** an update can break it. The guard switches itself off
  with an error in the log if the method disappears. After every ComfyUI update,
  follow [docs/comfyui-updates.md](docs/comfyui-updates.md).
- **TTS Audio Suite:** IndexTTS needs a local patch to fully leave the GPU on
  unload. Re-apply it after every suite update; see
  [docs/tts-audio-suite-vram.md](docs/tts-audio-suite-vram.md). Sending it
  upstream is planned in
  [docs/plans/2026-10-16-upstream-indextts-unload-fix.md](docs/plans/2026-10-16-upstream-indextts-unload-fix.md).
- **Env:**
  - `GPU_WORKLOAD_GUARD=0` disables it;
  - `GPU_WORKLOAD_GUARD_WAIT_S` sets how long it waits;
  - `GPU_WORKLOAD_BIN` points it at the CLI.

Measured 2026-10-13:

| Situation when a job was queued | Result |
| --- | --- |
| Strata loaded | job done in 10 s |
| Strata writing a 1,500-token answer | the answer completed; job done in 9 s |
| A Strata request between two jobs | both jobs succeeded |
| The guard's wait cut to 2 s | job failed visibly in 2.5 s |

## comfy-render: run a batch of workflows

```
comfy-render JOBS.json [--timeout SECONDS] [--json] [--verbose]
```

Queues every job up front, waits until **all** of them have finished, and
prints one line per job: the files it saved, or ComfyUI's own error. It doesn't
touch the GPU itself; the guard node does the handover. Installed as a symlink:
`~/.local/bin/comfy-render`.

JOBS.json is API-format workflow JSON, in one of three shapes:

| Shape | Use |
| --- | --- |
| a single workflow | one job |
| a list of workflows | several different jobs |
| `{"base": workflow, "jobs": [{"5.inputs.seed": 11}, …]}` | variants: each overrides `NODE.inputs.NAME` in a copy of `base` |

A UI-format export (`nodes` / `links`) is refused with a hint to use
"Export (API)".

What it reports:

| Outcome | Example line |
| --- | --- |
| saved files | `job 2/4 ok: /…/output/a.png` (files from any `Save*`/`Preview*` node, whatever its output key) |
| rejected at submit (HTTP 400) | `job 3/4 rejected: node 7 PreviewImage: Return type mismatch …` |
| failed while running | `job 1/4 failed: node 1 LoadImage: [Errno 2] No such file …` (`--verbose` adds the ComfyUI frames of the traceback) |
| finished, no files | `job 1/1 failed: finished but saved no files (no Save* output node?)` |
| cancelled while running | `job 2/4 cancelled: cancelled by the user (dashboard or ComfyUI) while running node 5 KSampler` |
| cancelled while queued | `job 3/4 cancelled: cancelled by the user (…) before it started` |

A bad job never stops the others. ComfyUI validates each output backwards and
stops at the first broken link, so a second problem in the same job only shows
up once the first is fixed.

Exit codes: `0` every job saved files · `1` some job was rejected or failed ·
`2` bad input file or ComfyUI unreachable · `3` no progress for `--timeout` s (default 1800) ·
`4` the user cancelled some job (wins over `1` and `3`).

**Cancelled jobs.** Nothing on this machine stops a job except a person (the
dashboard's Cancel button, ComfyUI's own page, or `comfy jobs
cancel`); the guard node fails a job with an error instead. So a cancel is
reported as the user's, separately from failures, and the summary ends with
`N job(s) cancelled by the user: ask them why before re-running`, so an agent
asks rather than retrying. ComfyUI records the two cases differently, and both
are handled:

- **Running:** `/interrupt` leaves an `execution_interrupted` entry in
  `/history`. Files the job saved before it was stopped are still listed.
- **Queued:** the job is deleted from the queue and never reaches `/history`.
  A job that is in neither `/queue` nor `/history` (read in that order, since
  ComfyUI moves a job from one to the other in a single step) was cancelled.
  Before this, comfy-render waited for it until the timeout.

**Live progress.** While jobs run it prints what each one is doing, read from
ComfyUI's websocket (the same feed as its own progress bar):

```
queued 1/1 job(s)
job 1/1 waiting to start (earlier jobs, or the GPU handover and model load)
job 1/1 running: node 3 CLIPTextEncode
job 1/1 running: node 5 KSampler step 57/150 (38%)
job 1/1 done (1 of 1 queued finished)
```

- **Throttled:** a line appears at most every 10 s per job, or at once when
  the running node changes.
- **Step counts** come from nodes that report them (samplers, and most video
  models' sampling); other nodes show just their name.
- **Fallback:** a line at least once a minute regardless, so a caller's idle
  watchdog (the chat bot's) doesn't kill a long render.
- **Pi passes these lines through as live steps**, so web and Slack show render
  progress as it happens.
- **Implementation:** Python's standard library has no websocket client, so
  comfy-render has a minimal one (local, receive-only, text frames only;
  preview-image frames are skipped). If it can't connect, the jobs still run
  and report normally, just without progress lines. `/history` remains the
  source of truth for when a job is finished.

**Timeout: no progress, not total time.** `--timeout` (default 1800 s) is how
long it may go without any progress: a job finishing, a websocket event for one
of its jobs (a node or sampler step starting), or ComfyUI's queue changing (the
batch may be waiting behind someone else's jobs, which send it no events). A
batch that keeps progressing may run for hours. On a timeout the unfinished
jobs stay in ComfyUI's queue.

Until 2026-10-16 it was a 3600 s limit on the whole batch. A 9-video batch hit
it with 6 jobs still running. pi's turn went on, Strata refused it (correctly,
since ComfyUI had jobs), pi retried for ~151 s and the turn failed with "GPU
busy". The same happened earlier that day with a 22-job batch. The pi skill
also now tells pi not to pipe comfy-render's output (it had used `| tail -20`,
which hid the live progress for the whole hour) or set its own timeout. pi's
bash tool has no default timeout.

**Why it exists:** a pi turn on 2026-10-13 hand-wrote a polling script that
crashed on `/history`'s shape after the first of four jobs finished. pi's turn
then continued while three jobs were still queued, so Strata was refused
(correctly) and the turn failed with "GPU busy", although all four images
rendered. Waiting correctly is the part a local model gets wrong; designing
the workflow is not.

Measured 2026-10-13 against the live ComfyUI:

| Test | Result |
| --- | --- |
| 3 seed variants of a base workflow | all saved, 6 s |
| a batch with one good job, an unknown node, a wrong link type, a missing model file | 1 ok, 3 rejected with ComfyUI's messages; the missing model listed the valid file names |
| a job whose input file vanished before it ran | `failed: node 1 LoadImage: [Errno 2] …`, while the other job in the batch still succeeded |
| a UI-format file, invalid JSON, a missing file | exit 2 with a one-line reason |
| a 150-step SDXL job | `KSampler step 9/150 (6%)`, then `64/150`, then `119/150`, 10 s apart |
| a job submitted with ComfyUI's models unloaded | `waiting to start …` at 0 s, then `CLIPTextEncode` at 26 s (model load), then the sampler steps |
| the websocket unreachable | no progress lines; the job still completed and reported normally |

## pi skill: run ComfyUI work through comfy-render, in batches

`pi/skills/comfyui-batching/SKILL.md` tells pi two things:

- run workflows with `comfy-render` rather than its own polling script;
- batch as a best effort: one call per round, over-generate rather than
  re-roll, one contact sheet per round.

Every handover costs a ~30 s Strata reload plus a re-read of the conversation.
pi still designs the workflows itself. The skill is installed for pi only, as a
symlink: `~/.pi/agent/skills/comfyui-batching`. Pi lists it in
`<available_skills>` in headless runs too (checked 2026-10-13).

**pi's retry window** (machine config, not in this repo): when ComfyUI is
still rendering, Strata refuses pi with a 503 and pi retries. The default is 3
retries, ~14 s in total, which a render that's still finishing outlasts.
`~/.pi/agent/settings.json` sets

```json
"retry": {"maxRetries": 8, "baseDelayMs": 2000, "maxAgentDelayMs": 30000}
```

`maxAgentDelayMs` caps each delay, not the total. Measured 2026-10-13 against an
always-503 server: delays of 2, 4, 8, 16 and then 30 s ×4, giving up after
151 s.

## `hold`: optional, for batches

`hold` is no longer needed for safety; the guard covers every job. It's a speed
optimisation: it keeps the GPU on comfy for a whole batch, so a Strata request
arriving between jobs is refused instead of costing two ~30 s handovers.

```sh
gpu-workload hold comfy --wait 600 -- ./render.sh
```

`hold` switches to comfy, then keeps the switch lock marked `hold comfy` until
COMMAND exits, so every Strata load is refused for that whole time.

- Inside a hold, `gpu-workload switch comfy` is a no-op, and `switch strata`
  fails. Both are detected via the `GPU_WORKLOAD_HOLD` env var, since taking
  the lock would deadlock.
- SIGINT, SIGTERM and SIGHUP are forwarded to COMMAND. The lock is released
  only after COMMAND exits.
- A script that queues **all** its jobs at once gets the same effect without
  `hold`, because the queue stays busy until they're done.
- A `hold` inside another `hold` just runs its command, since the outer one
  already owns the lock.

`gpu-workload check strata|comfy` is still there for scripts that want to
assert a mode.

## Strata hook: every model load goes through gpu-workload

Strata's config runs `gpu-workload hook strata-before-load` before it loads its
model. It sets `before_load_strict: true`, so a non-zero exit means Strata
answers **503** and doesn't load. The hook decides like this:

| Situation | Decision |
| --- | --- |
| a `switch strata` is in progress (it triggered this load) | allow |
| a `switch comfy` or `hold comfy` is in progress | refuse |
| a request woke the lazy model; ComfyUI has jobs running or queued | refuse: a render is never interrupted |
| a request woke the lazy model; ComfyUI idle, no models loaded | allow |
| a request woke the lazy model; ComfyUI idle but **holding models** | free ComfyUI's models (`/free`, wait up to 20 s for its VRAM to drop below 1500 MiB), then allow |
| either of the two above, and ComfyUI's VRAM won't drop or its RAM + swap is still above 4 GiB | also [restart ComfyUI](#comfyui-restart-ram-not-just-vram) first, then allow |

So whoever calls Strata, whether the chat bot, the `pi` CLI, curl or
Strata's web page, gets the GPU back whenever ComfyUI is merely idle. Nobody
has to run `switch strata` first.

**How the hook frees ComfyUI:** it does that itself rather than calling
`switch strata`, which ends in Strata's `/load` and would wait on the very load
running the hook. It holds the switch lock while it works, so a
`switch comfy` / `hold comfy` starting at the same moment waits for it. If a
ComfyUI job is queued between the hook's queue check and its `/free`, ComfyUI
postpones the free past that job. The VRAM wait then times out and the hook
refuses: Strata answers 503, it doesn't collide.

Measured 2026-10-13: a stray request with ComfyUI idle and holding 7 GB of
SDXL was answered in 31 s (~2 s free plus ~26 s warm load).

**How the hook tells a switch-triggered load from a stray one:** while a switch
runs, it holds an flock on `~/.local/state/gpu-workload/switch.lock` and writes
its target mode into that file. The hook never *waits* on that lock. If it did,
it would deadlock: `switch strata` holds the lock while it blocks on Strata's
`/load`, and `/load` is what runs the hook.

**Unloads aren't hooked.** Strata has no after-unload hook, so `idle_unload_s`
is left unset. A Strata that's still loaded simply shows as `strata` mode until
`switch comfy`.

Leaving ComfyUI idle with a model loaded doesn't reserve the GPU: the next
Strata request evicts it, and ComfyUI reloads it on its next job. ComfyUI's own
side is covered by the guard node above.

## ComfyUI restart: RAM, not just VRAM

`/free` empties ComfyUI's VRAM but not its RAM. On 2026-10-16, after a video
render, ComfyUI went from 34 GiB to 7 GiB of RAM plus 3.8 GiB of swap with no
models loaded. Strata then loaded its ~38 GiB of experts on top: the user
session's memory pressure reached 64%, Strata's load took 70 s instead of
23 s, and systemd-oomd killed `chat-bot.service` (which restarted).

So before Strata loads, whether from `switch strata` or the hook,
gpu-workload restarts ComfyUI (`systemctl --user restart comfyui`) when its
queue is empty (the existing rule: a render is never interrupted) **and**
either:

- its RAM + swap is above `GPU_WORKLOAD_COMFY_RESTART_MIB` (default 4096). RAM
  here is the unit's anonymous memory from its cgroup; page cache is left out,
  since the kernel drops it under pressure; or
- `/free` didn't get its VRAM below 1500 MiB, e.g. an extension that doesn't
  unload fully, like IndexTTS without
  [our patch](docs/tts-audio-suite-vram.md). Before, Strata was refused until
  someone restarted ComfyUI by hand. If a job was queued meanwhile (ComfyUI
  postpones `/free` past it), it still refuses instead.

**History.** A restart wipes ComfyUI's `/history`, which comfy-render polls every
2 s to report results. So it waits until the newest history entry finished
at least 10 s ago, then re-checks the queue and gives up (Strata answers 503)
if a job arrived meanwhile. A dashboard's list of recent
jobs also starts empty after a restart.

**Cost.** About 15–20 s more for that handover (ComfyUI starts in ~13 s),
only when ComfyUI is heavy. Strata's `before_load_timeout_s` is 90 to fit it.
ComfyUI reloads its models on its next job either way.

**Tested with a fake ComfyUI unit** (2026-10-16):

| Fake ComfyUI | Result |
| --- | --- |
| RAM over the threshold, last job finished 4 s ago | waited 4 s, restarted |
| RAM under the threshold, VRAM clean | left alone |
| a job queued during the wait | not restarted; failed with that reason |
| VRAM stuck at 2066 MiB after `/free`, queue empty | restarted |

**Measured on the real ComfyUI** (2026-10-16, Strata IQ2_XS, 128K context):

| Test | Result |
| --- | --- |
| Strata woken by requests after video renders, ComfyUI holding 6.1, 7.6 and 6.3 GiB of RAM + swap (19:21, 19:23, 19:56) | each time: waited 6–8 s for history, restarted, ComfyUI answered ~10 s later, Strata loaded in 33–43 s; no oomd kill |
| `comfy-render` of one SDXL job, then `switch strata` 1.5 s after queueing it (threshold lowered to 1024 MiB for the test) | the job finished, the switch waited 10 s, restarted ComfyUI (back in 11 s), loaded Strata; whole switch 57 s. comfy-render still reported the job `ok` with its file |
| After the restart | ComfyUI 0.8 GiB RAM, guard `on` in its log; a smoke job then took the GPU back from Strata and succeeded in 10.9 s |

`gpu-workload status` shows the RAM picture:

```
ram:    26.9 of 60.5 GiB available, swap 4.5 of 8.0 GiB used (strata 0.1 GiB + 0.2 swap, comfy 29.7 GiB + 3.6 swap)
```

`--json` has the same under `ram` (`total_mib`, `available_mib`,
`swap_total_mib`, `swap_used_mib`, and `strata` / `comfy` each with `ram_mib`,
`swap_mib` and `file_mib`, the page cache charged to the unit).

**Open question (2026-10-16).** At 20:00 systemd-oomd killed `strata.service`
itself during a load that skipped the restart: ComfyUI had just rendered a
video and was under the threshold (it was at ~3.7 GiB RAM + swap minutes
later). Every load that day without a restart (16:15, 20:00) ended in an oomd
kill; every one with a restart (19:21, 19:23, 19:56, 20:14) didn't. So the
4 GiB threshold, or counting only anonymous memory, may be too lenient. Each
hook-allowed load now logs `available_mib` and ComfyUI's `ram_mib` /
`swap_mib` / `file_mib` in the event log; the next kill will show which. If
that's still unclear, the simple fix is to always restart a ComfyUI that has
run a job since its last start.

## Event log

`~/.local/state/gpu-workload/events.jsonl` (last 500 lines) records every mode
change made by `switch`, every `hold` / `hold-end` (with COMMAND's exit code),
and every load decision the hook makes. Hook decisions carry a `source`
(`hook` = during a switch or hold, `request` = a request that woke Strata) and
a `reason`, e.g. `ComfyUI idle; freed its 7044 MiB of models`. `status`
shows the latest event on its `last:` line, and `--json` includes it as
`last_event`.

## Strata config this relies on

Each `~/Strata/strata-<size>.json` ([see strata-model](#strata-model-which-model-size-strata-serves);
`strata-iq2_xs.json` and `strata-iq3_xxs.json` as of 2026-10-16) has `"lazy_load": true`, so
`strata.service` can stay enabled without holding VRAM until something asks it
to load. It also has `"min_free_vram_mib": 14000`: a stray request that arrives
while ComfyUI holds models gets a 503 instead of loading on top of it. The file
before this change is `strata-iq2_xs.json.pre-lazy`.

It also has `before_load` (the hook above), `before_load_strict: true` and
`before_load_timeout_s: 90` (30 until 2026-10-16; raised for the ComfyUI
restart, backup `strata-iq2_xs.json.pre-restart`). The pre-hook config is the
same `strata-iq2_xs.json.pre-lazy` backup, taken before any of these keys were
added.

## strata-model: which model size Strata serves

```
strata-model                   # list installed sizes, * = active
strata-model iq3|iq2|SIZE      # switch (SIZE as in strata-SIZE.json)
strata-model iq3 --wait 120    # let an answer Strata is writing finish first
```

Strata serves one model per server, and it answers whatever model name a
request uses. So picking a model in pi doesn't change which size runs; the
server's config does. `strata.service` runs `--config strata-active.json`, a
symlink to one `strata-<size>.json` that Strata's setup wrote.

Switching unloads Strata if it's loaded (refused while it answers, unless
`--wait`), repoints the symlink and restarts the unloaded server (~3 s). The
model then loads on the next request, through the `before_load` hook as
always. It also sets pi's `defaultModel` to the new size, so pi's labels match
the server. the chat bot names its pi model in `the chat bot's config`
(`PI_DEFAULT_MODEL`); that one needs editing and a restart of the chat bot.

Each `strata-<size>.json` needs its own copy of the hook, `lazy_load` and the
other keys below. Strata's setup keeps them when it rewrites an installed
size's config, but **not** when it installs a new size: on 2026-10-16 the new
`strata-iq3_xxs.json` had none of them, and without the hook Strata would load
on top of ComfyUI. After installing a size, copy `allowed_hosts`,
`trusted_origins`, `open_browser`, `lazy_load`, `min_free_vram_mib`,
`before_load`, `before_load_strict` and `before_load_timeout_s` from another
size's config before switching to it.

Measured 2026-10-16, IQ3_XXS (128K context, images on the CPU): first load from
disk 38 s including a short answer; 82–97 tokens/s on a 700-token story (IQ2_XS:
~100); memory pressure peaked at 49% during the load (oomd acts above 50% for
20 s), with 14 GiB available once loaded. Installed as a symlink: `~/.local/bin/strata-model`.

## Env overrides

`GPU_WORKLOAD_STRATA_URL`, `_COMFY_URL`, `_STRATA_UNIT`, `_COMFY_UNIT`,
`_STRATA_PROC`, `_COMFY_PROC` (substrings of the nvidia-smi process path),
`_COMFY_IDLE_MIB`, and `_COMFY_RESTART_MIB`.

Installed as a symlink: `~/.local/bin/gpu-workload`.
