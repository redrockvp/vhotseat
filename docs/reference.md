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
vhotseat autostart install [--command web|record]  keep the dashboard (or the recorder) up from logon
vhotseat autostart status | remove              is it registered? · take it back out
vhotseat cost [--since 24h] [--json]            what the card's power has cost (needs power.price_per_kwh)
vhotseat why [NAME] [--json]                    why a workload can or can't take the GPU now: a dry run of the handover
vhotseat claims [--json]                        what owns the card by right (a game), and what matches now
vhotseat watch                                  log when a claim starts and ends, and sample, until stopped
```

Exit codes: `0` ok · `1` switch failed, gate refused, or `autostart status` with
nothing registered · `2` usage or bad config. `hold` exits with COMMAND's own
exit code once COMMAND has started. `why` and `claims` are reports: they exit `0`
whatever they find, including a card somebody else owns.

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

A floor is what a model needs *room to load into*, so it is never applied to a workload
whose weights are already on the card: its own memory doesn't count against it. Without
that, a second request reaching a server while the first one's weights are still streaming
in reads that 20 GB as the blocker, and refuses the load that put it there.

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

## Talking to a server that isn't a plain server

Two things a URL doesn't cover:

- **A key.** `headers:` are sent with every request vhotseat makes to that workload:

  ```yaml
  headers: {Authorization: "Bearer ${UNSLOTH_KEY}"}
  ```

  `$NAME` and `${NAME}` read the environment, because a key doesn't belong in a config file
  that gets pasted into terminals and committed. If the name isn't in the environment,
  nothing is invented for it: `status` says *asks for a key it isn't being sent (401)* and
  `doctor` fails naming the variable. A 401 is reported as **answering and refusing**, never
  as *not answering*: blurred together, a key that rotated looks like a server that's down,
  and every handover starts looking for a dead port.
- **A named unload.** Some servers release one model at a time and want its id in a body.
  `body:` provides it, with `{model}` filled from what `model:` says is served and `{wait}`
  from the release's wait:

  ```yaml
  release:
    post: /api/inference/unload
    body: {model_path: "{model}"}
  ```

  With nothing loaded there's no id to send, and an empty one is either a 422 or a success
  that did nothing: the ask is skipped and logged (`no model loaded: nothing to release`).
  If the workload says something *is* loaded and `model:` can't name it, the release fails
  and names the key to fix, rather than reporting success over a model that stayed on the
  card.

- **A sizing question.** `estimate:` asks the server what one of its models would cost on
  the card, at a context you name:

  ```yaml
  estimate:
    from: {get: /settings/last-model}
    post: /estimate-memory
    body: {model_path: "{id}", gguf_variant: "{gguf_variant}", n_ctx: "{ctx}"}
    ctx: 32768
    path: gpu_bytes
  ```

  `from:` reads the response that names the model and its fields fill the body; a
  placeholder with nothing behind it drops its key rather than sending the placeholder text.
  The answer is **reported, never enforced**: `status` says *X sizes at 19865 MiB* and `why`
  adds *more than the card's 24576 MiB: only part of it can be resident*. A server can fit a
  model by shrinking its context or spilling into RAM, and refusing on the number would be
  vhotseat holding an opinion about a trade its owner is making.

A workload with nothing inside it that can ask for the GPU - no hook, no plugin - is
declared `gate: {kind: hold}`: it is guarded only while it runs under `vhotseat hold`, so
the habit is to take the card for it (`vhotseat switch NAME`) before loading anything there.

## The ComfyUI guard

[integrations/comfyui/vhotseat_guard](../integrations/comfyui/vhotseat_guard)
wraps `execution.PromptExecutor.execute_async`, which ComfyUI's prompt worker
thread calls once per job, and runs `vhotseat switch comfy --wait 300` first.
The worker thread may block, so the web server stays responsive.

**Install** it from the latest GitHub release (run again to update), then
restart ComfyUI:

```sh
curl -fsSL https://github.com/wandermyz/vhotseat/releases/latest/download/vhotseat_guard.zip -o /tmp/vhotseat_guard.zip
unzip -o /tmp/vhotseat_guard.zip -d ~/ComfyUI/custom_nodes/
```

Each release carries `vhotseat_guard.zip` and `vhotseat_guard-<version>.zip`;
both unpack to a `vhotseat_guard/` folder. From a checkout, symlink
`integrations/comfyui/vhotseat_guard` into `custom_nodes/` instead.

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
- `--comfy-dir`, else `$COMFY_DIR`, else ComfyUI's own folder, read from its
  `custom_nodes` path (a model folder alone can be an `extra_model_paths.yaml`
  collection on another drive): used to print saved files as absolute paths.
  Without it, paths are relative to ComfyUI's folder (`output/…`).
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
- **Holders**: every process holding card memory, named, with how much, and which
  workload it belongs to (or *unmanaged*: a desktop, a browser, a game, an app you
  started yourself). `status` prints the five biggest on its `holds:` line, and
  `status --json` carries them as `holders: [{pid, mib, name, workload}]`.
- **GPU load**: the card's model, load, temperature and power, now and over time.
- **VRAM** and **RAM / Swap**: per workload, stacked against the totals.
- **Power cost**: what the card's watts cost across the range you're looking at, in
  total and per holder, plus what it's drawing right now. Needs `power.price_per_kwh`.
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

```sh
vhotseat autostart install          # Task Scheduler at logon on Windows, a systemd user unit elsewhere
vhotseat autostart status           # registered? running? (exit 1 when nothing is registered)
vhotseat autostart remove
```

`--command record` installs the sampler without the page. It registers one thing
only, named `vhotseat`; everything else in vhotseat stays per-command on purpose.
The unit it writes elsewhere, if you'd rather keep it by hand:

```ini
# ~/.config/systemd/user/vhotseat.service   (or `vhotseat autostart install` writes this)
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

```yaml
power:
  price_per_kwh: 4.11   # what one unit of electricity costs where this machine is
  currency: "R"         # written in front of the numbers
  note: "eThekwini (Durban) home 2026/27: 357.47 c/kWh ex VAT"   # shown under the tiles
```

With a price set, `vhotseat cost` and the **Power cost** card integrate the sampled
wattage into kWh and money, per holder: each gap between two samples that both
carry a wattage counts, charged to whoever held the card at the start of it. A
stretch with no samples is no data rather than a card that drew nothing, so it is
skipped, and the report says how much of the window had samples in it - with
`vhotseat autostart install` the sampling is continuous and the number means what
it says. What it counts is the card's draw as the driver reports it: the rest of
the machine, and what the PSU burns getting those watts there, are not in it, so
read it as a floor. At 300 W and R4.11 a unit that is R1.23 an hour, about R30 a
day, about R890 a month - which is the case for unloading a model you aren't using.

**`why [NAME]`** is the handover read backwards. Where `switch` acts, `why` reports:
for each workload it walks the same preconditions - every other workload that would
be released, the ones that would have to be waited for, then the admission floors -
and says which of `could take the card now`, `would have to wait` or `is blocked`
applies and why. The blocked reasons are the ones that make a handover fail: a
workload whose API is dead while its VRAM is still on the card (start it so there is
something to ask, or kill the process), a process holding memory with its service
stopped, and an admission floor the card can't reach - shown as arithmetic, since
"needs 14,000 MiB free, 900 after everything releasable is released" is a diagnosis
where "refused" is a mystery. The memory that can't be freed is named: the processes
vhotseat doesn't manage and how much each holds. `why` releases nothing, loads
nothing and restarts nothing; it exits 0 even when the answer is `blocked`, because
it is a report and not a gate. `--json` gives the same per workload
(`{would, holder, free_mib, resident, notes[]}`); the dashboard shows it as the
**Next handover** card.

**Claims: a game owns the card.** `vhotseat claims` prints what the rules match right
now and the rules themselves; the same scan runs inside every gate and guard decision,
so nothing resident is needed for the rule to hold (docs/how-it-works.md §3.7).

```yaml
claims_builtin: true        # the built-in rules for the usual installations (default true)
claims:
  - path: '*\World of Warcraft\*'      # matched against the process's image path
    note: World of Warcraft            # how it's described in a refusal
  - name: rocketleague.exe             # or by exe name: works when the path can't be read
  - parent: mylauncher.exe             # or by any ancestor's exe name (its helpers too)
```

While something matches, nothing **new** loads: the gate refuses a load a request would
wake, and `switch --request` - what ComfyUI's guard node uses, because the queue is
asking on somebody's behalf - is refused too. A `switch` you type yourself is an order
and is not refused; neither is a requested switch that would load nothing, so a batch
mid-flight carries on. Nothing loaded is interrupted, and no claimed process is touched.
Built-ins know Steam (`*\steamapps\common\*`), Epic, Riot, anything under a `Games`
folder, and a handful of exes; launchers and their helpers never claim the card, because
they run all day. `claims_builtin: false` leaves only your rules.

**`vhotseat watch`** adds the record, not the rule: `claim` and `claim-end` rows in the
event log, one per kind of process rather than one per pid (a game is twenty), plus the
telemetry sampling. It's the cheapest thing to keep up from logon:
`vhotseat autostart install --command watch`. Where the dashboard already runs, it is the
recorder itself: `vhotseat web` probes the machine often enough to notice a claim start
and end, so a box with the dashboard up needs nothing else.

**Disk:** about 6 MiB per day of retention with two workloads at the default
5 s interval (~305 bytes a sample, 17,280 a day, plus events and logs), so
~6 MiB for the default 24 h, ~43 MiB for a week. Each further workload adds
~0.8 MiB a day. The file stays at that size: deleted rows' space is reused.
Several processes sampling at once don't double up. Telemetry never gets in
a handover's way: a write waits at most 2 s, and any error is ignored.
