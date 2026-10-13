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

## For workflows

**ComfyUI workflows must run under `hold`**, because an *idle* ComfyUI no
longer reserves the GPU (see the hook below):

```sh
gpu-workload hold comfy --wait 600 -- ./render.sh
```

`hold` switches to comfy, then keeps the switch lock marked `hold comfy` until
COMMAND exits, so every Strata load is refused for that whole time. Without
it, a script that queues jobs one at a time leaves ComfyUI's queue empty
between jobs, and a stray Strata request could take the GPU in that gap.

- Inside a hold, `gpu-workload switch comfy` is a no-op, and `switch strata`
  fails. Both are detected via the `GPU_WORKLOAD_HOLD` env var, since taking
  the lock would deadlock.
- SIGINT, SIGTERM and SIGHUP are forwarded to COMMAND. The lock is released
  only after COMMAND exits.
- A script that queues **all** its jobs at once is also safe without `hold`,
  because the queue stays busy until they're done.

Strata-side callers need no guard: a request simply wakes Strata (see below).
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

ComfyUI has **no hook yet**. Something that queues ComfyUI jobs directly, while
Strata is loaded and not under `hold`, would still collide. That includes
generating from ComfyUI's own web page. Leaving ComfyUI idle with a model loaded
also no longer reserves the GPU: the next Strata request evicts it, and
ComfyUI reloads it on its next render.

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
