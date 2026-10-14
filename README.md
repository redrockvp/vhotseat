# gpu-workload

Hands the single 16 GB GPU between **Strata** (local LLM, `strata.service`,
`:2335`) and **ComfyUI** (`comfyui.service`, `:8188`). They can't both hold VRAM.

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
| `switch strata` | Waits for ComfyUI's queue to drain (fails at once unless `--wait N`), `POST /free`, waits until ComfyUI's VRAM drops below 1500 MiB, then `POST /load` on Strata, which blocks until the model is up |
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

A bad job never stops the others. ComfyUI validates each output backwards and
stops at the first broken link, so a second problem in the same job only shows
up once the first is fixed.

Exit codes: `0` every job saved files · `1` some job was rejected or failed ·
`2` bad input file or ComfyUI unreachable · `3` timed out (default 3600 s).
While waiting it prints a line at least once a minute, so a caller's idle
watchdog (the chat bot's) doesn't kill a long render.

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

## Event log

`~/.local/state/gpu-workload/events.jsonl` (last 500 lines) records every mode
change made by `switch`, every `hold` / `hold-end` (with COMMAND's exit code),
and every load decision the hook makes. Hook decisions carry a `source`
(`hook` = during a switch or hold, `request` = a request that woke Strata) and
a `reason`, e.g. `ComfyUI idle; freed its 7044 MiB of models`. `status`
shows the latest event on its `last:` line, and `--json` includes it as
`last_event`.

## Strata config this relies on

`~/Strata/strata-iq2_xs.json` has `"lazy_load": true`, so
`strata.service` can stay enabled without holding VRAM until something asks it
to load. It also has `"min_free_vram_mib": 14000`: a stray request that arrives
while ComfyUI holds models gets a 503 instead of loading on top of it. The file
before this change is `strata-iq2_xs.json.pre-lazy`.

It also has `before_load` (the hook above), `before_load_strict: true` and
`before_load_timeout_s: 30`. The pre-hook config is the same
`strata-iq2_xs.json.pre-lazy` backup, taken before any of these keys were
added.

## Env overrides

`GPU_WORKLOAD_STRATA_URL`, `_COMFY_URL`, `_STRATA_UNIT`, `_COMFY_UNIT`,
`_STRATA_PROC`, `_COMFY_PROC` (substrings of the nvidia-smi process path), and
`_COMFY_IDLE_MIB`.

Installed as a symlink: `~/.local/bin/gpu-workload`.
