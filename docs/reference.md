# Reference

Commands, config, gates, admission, the ComfyUI guard, `comfy-render` and the
dashboard, in detail. For the design behind them see
[how-it-works.md](how-it-works.md); to set things up, the
[quick start](guides/quickstart-strata-pi-comfyui.md).

## Commands

```
vhotseat status [--json]                        who owns the GPU, what each workload holds, last event
vhotseat switch NAME|none [--wait S] [--json]   give the GPU to NAME, or free it; idempotent
vhotseat hold NAME [--wait S] -- COMMAND...     give it to NAME and keep it until COMMAND exits
vhotseat gate NAME                              for hook gates: may NAME load? (run by the server)
vhotseat doctor [--json]                        check the config, the GPU, every workload and gate
vhotseat doctor --exercise NAME [--wait S]      run a real cycle on NAME, timed
vhotseat web [--host H] [--port P]              dashboard: RAM and VRAM over time, handovers, events, logs
vhotseat record                                 sample RAM and VRAM into the telemetry until stopped
```

Exit codes: `0` ok · `1` switch failed or gate refused · `2` usage or bad
config. `hold` exits with COMMAND's own exit code once COMMAND has started.

**Modes.** `status` reports the owner, derived live from each workload's API
and the GPU's per-process memory; there is no state file to drift. The mode is
a workload's name, `none` (the GPU is free) or `conflict` (two hold memory at
once; a `switch` either way resolves it).

**`switch NAME`** releases every other workload, then loads NAME if it can be
loaded ahead of a request. A workload that's working makes it fail at once,
unless `--wait S` lets that work finish first. A stopped service is started.
`switch none` frees the GPU without picking an owner.

**`hold NAME -- COMMAND`** switches, then keeps the GPU for NAME until COMMAND
exits: every other workload's gate refuses meanwhile. Inside it, `switch NAME`
and a nested `hold` are no-ops (`VHOTSEAT_HOLD` is set in COMMAND's
environment), and SIGINT, SIGTERM and SIGHUP are passed on to COMMAND. Use it
for a batch that must not be split, or as the gate for a program with no
server.

**Config** is `~/.config/vhotseat/config.yaml`, or `$VHOTSEAT_CONFIG`. It's
validated on load and every error names the key. A broken config makes every
gate refuse rather than letting a load through unchecked.

**State** is in `~/.local/state/vhotseat/` (`$XDG_STATE_HOME`): the lock
(`switch.lock`, one per GPU) and the event log (`events.jsonl`, the last 500
decisions: every switch, hold and gate, with its reason), and the telemetry
(`telemetry.db`, below).

## Gates: how a workload asks for the GPU

| Gate | For | How |
| --- | --- | --- |
| `hook` | a server that runs a command before it loads (Strata's `before_load`) | runs `vhotseat gate NAME`; exit 0 = load, 1 = refuse |
| `plugin` | a server with a plugin system that runs before each job (ComfyUI) | the plugin runs `vhotseat switch NAME --wait N`; the job fails visibly if that fails |
| `proxy` | a server reached only over HTTP, with no hook (Ollama, llama-server) | a small proxy in front of it asks before forwarding; [build one with the guide](guides/proxy-gate.md) |
| `hold` | a program that isn't a server (a training run, a script) | always run it as `vhotseat hold NAME -- CMD` |

A gate runs **inside** its workload's own load, so it never waits on the lock:
a `switch` holding it may be waiting on this very load. It allows a load that a
`switch` or `hold` for this workload asked for; refuses while any other one is
in progress; and otherwise releases the other workloads itself and allows. If
another workload is working (ComfyUI has a job queued, say), or its state can't
be read, the gate refuses: a render is never interrupted.

To add a gate for a new server, see [guides/driver-authoring.md](guides/driver-authoring.md#4-pick-the-gate)
and [guides/proxy-gate.md](guides/proxy-gate.md).

## Admission

Releasing the others always happens. Admission decides whether that's enough
for the incoming workload to load safely:

```yaml
admission:
  min_available_ram_mib: 46000   # MemAvailable: free RAM plus cache the kernel can drop
  min_free_vram_mib: 14000       # covers VRAM vhotseat doesn't manage (the desktop, a game)
  by_model:                      # per model, when the server says which it will load
    big-model-iq3: {min_available_ram_mib: 46000}
```

It matters for MoE LLMs that keep their experts in RAM. ComfyUI's `/free`
empties its VRAM but not its RAM: after a video render an idle ComfyUI can hold
6–15 GiB of RAM and swap, and an LLM loading on top of that can be killed by
the OOM killer. If admission isn't met after the release, vhotseat restarts
idle workloads that allow it (`allow_restart: true`); if it still isn't met,
the load is refused and the event log says what was short.

A ComfyUI restart wipes its `/history`, so it only happens with an empty queue,
and once the latest result is at least 10 s old (`history_settle_s`), so
clients reading results have had time to.

## The ComfyUI guard

[integrations/comfyui/vhotseat_guard](../integrations/comfyui/vhotseat_guard)
wraps `execution.PromptExecutor.execute_async`, which ComfyUI's prompt worker
thread calls once per job, and runs `vhotseat switch comfy --wait 300` first.
The worker thread may block, so the web server stays responsive.

- **On failure** the job is **not run** and fails visibly: an `execution_error`
  "GPU busy, job not run: <reason>" in the UI and in `/history`.
- **It wraps a private API,** so a ComfyUI update can break it. If the method is
  missing, the guard logs an error and stays off rather than breaking ComfyUI.
  After every update, follow [comfyui-updates.md](comfyui-updates.md).
- **Env:** `VHOTSEAT_GUARD=0` disables it; `VHOTSEAT_GUARD_WAIT_S` sets how
  long a job waits for the LLM to finish an answer (default 300);
  `VHOTSEAT_BIN` points it at the CLI.

A custom node that doesn't release its VRAM on `/free` makes every handover
fail. [examples/vram-leak-tts-audio-suite.md](examples/vram-leak-tts-audio-suite.md)
is a worked example of finding and fixing one; `patches:` in the config lets
`doctor` notice when an update drops a local fix.

## comfy-render: run a batch of workflows

```
comfy-render JOBS.json [--url URL] [--comfy-dir DIR] [--timeout SECONDS]
                       [--json] [--verbose] [--hold [NAME]] [--hold-wait SECONDS]
```

Queues every job up front, waits until **all** of them have finished, and
prints one line per job: the files it saved, or ComfyUI's own error. It doesn't
touch the GPU; the guard does the handover. It works with ComfyUI alone.

**Why:** while ComfyUI's queue is non-empty, every LLM load is refused, so a
batch costs 2 handovers instead of 2 per job. And the agent stays blocked in
the command, so it can't ask the LLM anything mid-batch. Waiting correctly is
the part a local model tends to get wrong when it writes its own polling
script.

JOBS.json is ComfyUI API-format workflow JSON ("Export (API)"), in one of
three shapes:

| Shape | Use |
| --- | --- |
| a single workflow | one job |
| a list of workflows | several different jobs |
| `{"base": workflow, "jobs": [{"5.inputs.seed": 11}, …]}` | variants: each overrides `NODE.inputs.NAME` in a copy of `base` |

A UI-format export (`nodes` / `links`) is refused with a hint.

| Outcome | Example line |
| --- | --- |
| saved files | `job 2/4 ok: /…/output/a.png` |
| rejected at submit | `job 3/4 rejected: node 7 PreviewImage: Return type mismatch …` |
| failed while running | `job 1/4 failed: node 1 LoadImage: [Errno 2] No such file …` (`--verbose` adds the traceback's ComfyUI frames) |
| finished, no files | `job 1/1 failed: finished but saved no files (no Save* output node?)` |
| cancelled | `job 2/4 cancelled: cancelled by the user (ComfyUI or another client) while running node 5 KSampler` |

A bad job never stops the others. While jobs run, it prints live progress from
ComfyUI's websocket (`job 1/1 running: node 5 KSampler step 57/150 (38%)`), at
most every 10 s per job and at least once a minute, so a caller's idle
watchdog doesn't kill a long render.

Exit codes: `0` every job saved files · `1` some job was rejected or failed ·
`2` bad input file or ComfyUI unreachable · `3` no progress for `--timeout`
seconds (default 1800; the batch as a whole may take hours) · `4` the user
cancelled some job (wins over `1` and `3`: ask them why before re-running). On
a timeout the unfinished jobs are left in ComfyUI's queue.

- `--url`, else `$COMFY_URL`, else `http://127.0.0.1:8188`.
- `--comfy-dir`, else `$COMFY_DIR`, else the folder ComfyUI reports for its
  models: used to print saved files as absolute paths. Without it, paths are
  relative to ComfyUI's folder (`output/…`).
- `--hold [NAME]` runs the batch under `vhotseat hold NAME` (default `comfy`),
  so even unrelated traffic, another conversation say, can't take the GPU back
  between two jobs. `--hold-wait` is how long to wait for the current owner's
  work first (default 600 s).

## Dashboard and telemetry

```sh
vhotseat web                 # http://localhost:8765/ ; --host 0.0.0.0 to reach it from elsewhere (no auth)
```

One page, refreshed every 5 s, that works on a phone too:

- **Owner**: who holds the GPU now, each workload's state, and the owner over time.
- **GPU load**: the card's model, load, temperature and power, now and over time.
- **VRAM** and **RAM / Swap**: per workload, stacked against the totals.
- **Events / Logs**: every switch, hold and gate decision, and every line a
  vhotseat command logged, from any process (the guard's `switch comfy`,
  Strata's `gate strata`, your `hold`).

Every chart marks each event with a dashed line: hover it to see the event,
click it to open it in the table. Ranges run from 15 minutes to the whole
retention window. A button in the header switches the theme (system, light or
dark; kept in the browser). It only reads; it never switches anything.

![The dashboard on an RTX 5070 Ti: Strata hands the GPU to ComfyUI at 01:04, and a render is running.](https://raw.githubusercontent.com/wandermyz/vhotseat/24999a8e62e3a6342e9af6c3bc70042e77765c60/web-dashboard/desktop.png)

**History doesn't need the page open.** Every vhotseat command records into
`~/.local/state/vhotseat/telemetry.db` (SQLite) as it runs: each status probe
is a sample, each event and log line a row, and a `hold` or a waiting `switch`
samples every `interval_s` while it lasts. So a dashboard started later shows
what happened before. Between commands nothing samples, which shows as gaps;
for an unbroken history run the recorder as a service:

```ini
# ~/.config/systemd/user/vhotseat-record.service   (or run `vhotseat web` this way instead)
[Service]
ExecStart=%h/.local/bin/vhotseat record
Restart=on-failure
[Install]
WantedBy=default.target
```

```yaml
telemetry:
  retention_hours: 24   # default; rows older than this are deleted as new ones arrive
  interval_s: 5         # default
  # enabled: false      # record nothing; the dashboard shows the live status only
```

**Disk:** about 6 MiB per day of retention with two workloads at the default
5 s interval (~305 bytes a sample, 17,280 a day, plus events and logs), so
~6 MiB for the default 24 h, ~43 MiB for a week. Each further workload adds
~0.8 MiB a day. The file stays at that size: deleted rows' space is reused.
Several processes sampling at once don't double up. Telemetry never gets in
a handover's way: a write waits at most 2 s, and any error is ignored.
