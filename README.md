# gpu-workload

Hands the single 16 GB GPU between **Strata** (local LLM, `strata.service`,
`:2335`) and **ComfyUI** (`comfyui.service`, `:8188`). They can't both hold VRAM.

```
gpu-workload status [--json]
gpu-workload switch strata|comfy [--wait SECONDS] [--json]
gpu-workload check  strata|comfy [--json]
gpu-workload hook strata-before-load     # called by Strata, not by hand
```

Exit codes: `0` ok · `1` switch failed or hook refused · `2` usage · `3` `check` found the wrong mode.

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

Workflows are expected to guard themselves and fail on the wrong mode:

```sh
gpu-workload check comfy || exit 1     # before queueing ComfyUI jobs
gpu-workload check strata || exit 1    # before driving Strata
```

## Strata hook: every model load goes through gpu-workload

Strata's config runs `gpu-workload hook strata-before-load` before it loads its
model. It sets `before_load_strict: true`, so a non-zero exit means Strata
answers **503** and doesn't load. The hook decides like this:

| Situation | Decision |
| --- | --- |
| a `switch strata` is in progress (it triggered this load) | allow |
| a `switch comfy` is in progress | refuse |
| a stray request woke the lazy model, ComfyUI idle and holding no models | allow; mode becomes `strata` |
| a stray request, but ComfyUI has jobs or holds more than 1500 MiB | refuse |

**How the hook tells a switch-triggered load from a stray one:** while a switch
runs, it holds an flock on `~/.local/state/gpu-workload/switch.lock` and writes
its target mode into that file. The hook never *waits* on that lock. If it did,
it would deadlock: `switch strata` holds the lock while it blocks on Strata's
`/load`, and `/load` is what runs the hook.

**Unloads aren't hooked.** Strata has no after-unload hook, so `idle_unload_s`
is left unset. A Strata that's still loaded simply shows as `strata` mode until
`switch comfy`.

ComfyUI has **no hook yet**. Something that queues ComfyUI jobs directly, while
Strata is loaded, would still collide. Comfy workflows should
`gpu-workload check comfy` first.

## Event log

`~/.local/state/gpu-workload/events.jsonl` (last 500 lines) records every mode
change made by `switch`, and every load decision the hook makes, with its
`source` (`hook` = during a switch, `request` = stray) and `reason`. `status`
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
