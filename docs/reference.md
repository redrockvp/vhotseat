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
once; a `switch` either way resolves it). Its `policy:` line prints each
workload's priority and what it gives way for, so the two keys are visible next
to the memory they decide the fate of.

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
gate refuse rather than letting a load through unchecked. The dashboard's policy
endpoints write pieces of it - the two policy keys, `ask:` and `pressure:` - in
a way that leaves the rest of the file, comments included, exactly as it was
(`config_edit.py`), and keep the previous file as `config.yaml.bak-<time>`.

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

## Who gives way for whom

Which workloads get unloaded to make room used to be one rule for everything: whatever
held memory on the card was a candidate for every handover. Three keys per workload make
it a decision of yours instead:

```yaml
workloads:
  strata:
    priority: 60            # 0-100, default 50: lowest gives way first
    yields_to: [comfy, claims]   # ComfyUI and a game may unload it; anything else is refused
  unsloth:
    priority: 20            # idle and cheap to reload: asked first
    yields_to: ["*"]        # the default: it gives way for anyone
  comfy:
    yields_to: ["*"]        # makes room for everyone...
    asks_for: [claims]      # ...except a game, which asks first
```

`yields_to:` lists the newcomers this workload will make room for. An entry is another
workload's name, `claims` (a process that owns the card by right - a game: see Claims
below), `pressure` (the card running short under a process vhotseat doesn't manage), or
`"*"` for anyone. Absent means `["*"]`, which is exactly what vhotseat did before this
existed, so an existing config keeps behaving as it did. An empty list means it gives
way for nobody: only a command you type moves it. An entry naming a workload that isn't
in the config is a config error, not a rule that quietly never matches; a workload can't
be named `claims` or `pressure`, because those are words `yields_to:` already means.

`asks_for:` is the third answer: *ask me before you unload me*. It takes the same entries
as `yields_to:` without the `"*"` - an ask that means "anyone" is a question nobody can
answer - and one entry named in both keys is a config error, because it would say two
things at once. **The asking isn't built yet.** Until it is, a name in `asks_for:` keeps
that workload loaded exactly as leaving it out of `yields_to:` does - a load nobody chose
is refused, a claim leaves it holding and records `policy-kept` - and `vhotseat status`
and `vhotseat why` say it asks first rather than pretending an answer is coming. What is
not asked for is not unloaded, whichever way the question was meant.

Top level, `ask:` is what an unanswered ask will be worth: `timeout_s` (default 120) how
long to hold the handover for an answer, and `fallback` of `keep` (default, leave it
loaded) or `release` (unload it anyway). Nothing waits or notifies yet; the two keys are
read by the dashboard and recorded so the asking has something written to be built
against.

What the policy decides is **who gets unloaded, and who is refused instead**. It is
enforced wherever vhotseat decides to unload something, and its direction matters:

- a **load nobody chose** - Strata's gate, ComfyUI's guard (`switch comfy --request`), a
  proxy's `Seat.acquire` - is refused when a workload holding the card won't give way for
  it. The refusal names the holder and what its `yields_to:` allows, records a
  `policy-refused` row, and shows on the dashboard. Nothing is unloaded partially, because
  the card cannot be given to the newcomer anyway.
- a **`switch` you type** is an order, and goes through: it unloads the workload that
  wouldn't have, and records `policy-override` saying whose policy it overrode, so the log
  answers "why did Strata lose its 20 GB?" later.
- a **claim starting** (`on_start: release`) never unloads a workload whose `yields_to`
  doesn't cover `claims`. It isn't refused - freeing the card is not a load - the workload
  is simply not a target, and the record and the dashboard say so (`policy-kept`), because
  the game may now be short of the VRAM that stayed put.
- a workload at or below its `idle_vram_mib` holds nothing for anyone to be short of, so
  it doesn't refuse a load whatever its `yields_to:` says. The policy reads what the card
  shows; a load that hasn't put weights on the card yet isn't yet a holder.

`priority:` orders the workloads that *may* be unloaded - the lowest is asked first - and
never overrules `yields_to:` or `asks_for:`. All three are shown by `vhotseat status` and
`vhotseat why`, and edited through `GET`/`POST /api/policy`; the page that edits them is
still to come.

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
- **Who gives way for whom**: the policy as the config has it - each workload's priority,
  what it gives way for, what it holds on the card now and what the history has seen it
  holding at most - and for every pair of workloads a tile saying whether they fit on the
  card at the same time. Read-only on the page for now; `GET`/`POST /api/policy` (below)
  reads and writes the keys, and the page that edits them is the next part.
- **Events / Logs**: every switch, hold and gate decision, and every line a
  vhotseat command logged, from any process (the guard's `switch comfy`,
  Strata's `gate strata`, your `hold`).

When a policy refused a load, or a claim wasn't allowed to unload something, a banner at the
top of the page says so - the sentence from the event row, with its time - and stops once the
row is more than five minutes old. It reads the event log rather than anything the dashboard
remembers, so a refusal written by `vhotseat watch`, or by a guard in another process, warns
here too.

Every chart marks each event with a dashed line: hover it to see the event,
click it to open it in the table. Ranges run from 15 minutes to the whole
retention window. A button in the header switches the theme (system, light or
dark; kept in the browser). The page never switches or releases anything: the one
write it makes is the policy, and that goes into `config.yaml`, not into a
running process.

### Reading and saving the policy

| Route | Returns |
|---|---|
| `GET /api/policy` | what the grid needs: the workloads in the order a handover unloads them, the columns a pair can be asked for (each other workload, `claims`, `pressure`), each cell's choice (`yes` / `ask` / `keep`) with the sentence saying what it does and why, the `ask:` and `pressure:` numbers, and a `version` (sha256 of the config file) to send back with a save. Grid and version are read from the file in one go, and those rules are what this process then acts on: rules remembered from an older file beside a version read fresh is how a save would write someone's hand-edit away and report success. A file that stopped being a config keeps the rules that were in force and carries `config_error` in the answer |
| `POST /api/policy` | `{"version": "..."}` when the file now says what was posted. Body: `version` (required) and any of `priorities` (the unload order), `matrix` (`{workload: {requester: "yes"\|"ask"\|"keep"}}`), `ask`, `pressure` - a key left out is left alone. `409` with the version that's on disk when the file changed since it was read (or without one, when another save is midway through the file and this one should simply try again); `400` with what `doctor` would say, when the result wouldn't load |

The POST is guarded three ways, because a config is not a page-refresh: the
`X-Vhotseat-Edit: 1` header, `Content-Type: application/json` - a page on another origin
can set neither from a form, and to set both from a script it needs a preflight this server
never answers - and an `Origin` that is this dashboard's own port when an `Origin` is sent at
all. No CORS header is ever sent, so nothing here is readable from another origin either.

The file is rewritten a line at a time, only where a policy key lives: comments, key order,
quoting style and CRLF survive a save, a key that wasn't touched isn't rewritten, and the whole
result is checked by the same `config.parse` the CLI loads with before a byte is written. A key
that goes back to its default is dropped, since an absent key says it - unless the owner left a
note on that line, in which case the line stays, says the default, and keeps their note: the note is
the reason for a decision that was changed, and deleting the key with it would lose something the
owner wrote. The file that was there is kept beside it as `config.yaml.bak-<time>` (the newest ten),
and the save takes a turn on `config.yaml.lock` beside the config - the same operating-system lock
the handover takes - so two pages, or a page and a dashboard in another process, can't both save
from the same reading and quietly undo one another: whoever arrives second is refused and tries
again. `vhotseat doctor` and `git diff` still make sense of the file afterwards.
`vhotseat doctor` and `git diff` still make sense of the file afterwards.

A save reaches the processes that are already running. `vhotseat web`, `vhotseat watch` and
`vhotseat record` notice the file's mtime and size change - each samples through the same probe -
and put the new policy, `pressure:` and `ask:` numbers in force; every gate, guard and `switch` is a
fresh process that read the config as it started. Which workloads exist, how to reach them and what
claims the card are not reloaded - that is a restart.
If the file stops being a config, the rules already in force keep running and `/api/status` carries
`config_error` saying so: a config that doesn't read is not a reason to unload anything, and a
dashboard that quietly kept showing the old policy would be worse. `api.py`'s `Seat` - a proxy's
gate, opened once and asked before every request - reloads the same way, on each `acquire()`.

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
be released, the ones that would have to be waited for, the ones whose `yields_to:`
refuses to give way for it, then the admission floors - and says which of
`could take the card now`, `would have to wait` or `is blocked` applies and why. A
workload that won't give way is `blocked`, not `waiting`: nothing gets unloaded and
nothing loads. Each answer carries the workload's own `priority` and `yields_to`, and
while something claims the card it names any workload the claim won't unload, because
"the game is short of VRAM" is otherwise a mystery. The blocked reasons are the ones
that make a handover fail: a
workload whose API is dead while its VRAM is still on the card (start it so there is
something to ask, or kill the process), a process holding memory with its service
stopped, and an admission floor the card can't reach - shown as arithmetic, since
"needs 14,000 MiB free, 900 after everything releasable is released" is a diagnosis
where "refused" is a mystery. The memory that can't be freed is named: the processes
vhotseat doesn't manage and how much each holds. `why` releases nothing, loads
nothing and restarts nothing; it exits 0 even when the answer is `blocked`, because
it is a report and not a gate. `--json` gives the same per workload
(`{would, holder, free_mib, resident, priority, yields_to, asks_for, notes[]}`); the dashboard
shows it as the **Next handover** card.

**Claims: a game owns the card.** `vhotseat claims` prints what the rules match right
now, the rules themselves and what each one does when it appears, and - while a claim is
open - what has already been unloaded for it and what is still holding and why. The same
scan runs inside every gate and guard decision, so nothing resident is needed for the
rule to hold (docs/how-it-works.md §3.7).

```yaml
claims_builtin: true        # the built-in rules for the usual installations (default true)
claims:
  - path: '*\World of Warcraft\*'      # matched against the process's image path
    note: World of Warcraft            # how it's described in a refusal
  - name: rocketleague.exe             # or by exe name: works when the path can't be read
  - parent: mylauncher.exe             # or by any ancestor's exe name (its helpers too)
  on_start: release                    # per rule: release (default) or block, see below
claims_ignore: [myelectronapp.exe]     # names or path globs the built-in rules won't claim
claims_poll_s: 2                       # how often `watch` looks for a claim (default 2 s)
```

**What a claim does when it appears.** `on_start: release` (the default, and what the built-in
game rules use) frees the card for it: every idle workload vhotseat manages is asked to let go of
its model through the normal release path - API unload, then the card confirms the MiB went - and
the servers stay running. A workload with work in flight is never interrupted: it is skipped,
recorded as `claim-release-waiting`, and released on a later check as soon as it goes idle, while
the claim lasts. Which workloads the claim is owed is decided when its release first runs, from
what the card held then, and recorded: a model the owner loads afterwards - a typed `vhotseat
switch` is an order - is left alone, and a workload that held nothing to begin with isn't waited
on. A workload that says it unloaded and keeps holding its VRAM is asked three times and then left
for the rest of the claim. `on_start: block` is the older behaviour: nothing new loads, and what is
already loaded keeps what it has. Both are enforced at every gate whether or not `watch` runs;
`release` is the extra half, and it's why `vhotseat autostart install --command watch` is worth
having.

`claims_poll_s` is how often that watcher looks - a game has allocated within seconds of starting.
It's independent of `telemetry.interval_s`, which stays whatever it is for the history graphs.

**A claim that isn't allowed to unload something.** A workload whose `yields_to:` doesn't list
`claims` is not a target of the claim: it keeps its model, the claim goes ahead without it, and
the release is never refused - freeing the card is not a load, so a policy can only ever take a
workload off the list of what may be unloaded, never stop the release. Because the game is then
short of exactly the VRAM that stayed put, the decision is written down twice: a `policy-kept` row
in the event log (which the dashboard shows as a warning until it goes stale), and the workload
named as `kept for its policy:` in `vhotseat claims` and `vhotseat status`, next to what the claim
was given.

**Pressure: an app vhotseat doesn't manage takes the card.** A game names itself, and the claim
rules say what to do with it. A Blender render, a CUDA script, or a browser that decides to use the
GPU for something large does not: no rule covers it, nobody asked for the card, and the card is
short anyway. Pressure is how vhotseat notices that and answers with the only lever it has -
releasing workloads that are idle: the ones whose `yields_to:` covers `pressure`, lowest
`priority:` first, and no more of them than pays for the room. It needs a sampler running to say
"has been short for 30 s", so it works when `vhotseat watch`, `vhotseat web` or `vhotseat record`
is (which is what `vhotseat autostart install` starts); without samples, nothing fires.

```yaml
pressure:
  enabled: true              # false: never release for an app vhotseat doesn't manage
  min_free_mib: 512          # the card is out of working room below this...
  sustain_s: 30              # ...and has been for this long (6 samples at 5 s)...
  grow_mib: 2000             # ...and the memory vhotseat doesn't manage is this far over its floor
  headroom_mib: 1000         # release enough to get back to min_free plus this
  end_margin_mib: 2000       # the episode ends once free is back above min_free plus this
  cooldown_s: 600            # and the next episode starts no sooner than this after the last
  ignore: [vmware-vmx.exe]   # names or path globs that are never growers: a VM, a benchmark, a miner
```

All of it is on by default, and every number is deliberately wide, because a wrong firing costs
unloading ~20 GB, reloading it, and possibly a server restart. They come off a read-only
measurement of this box (2026-10-22, 99 samples over ~200 s with the current apps open: Strata
loaded with a 21394 MiB model, ComfyUI down, browser, Orca and Discord open): the card sat at
**779-958 MiB free** all through it, and the processes vhotseat doesn't manage held
**3096-3269 MiB** - dwm 1370, firefox 372, explorer 362, Discord 211, Orca ~186 - varying by
**173 MiB** in three minutes. So `min_free_mib: 512` is under this machine's ordinary bottom,
which means "low" is a change rather than the state of a card with a model on it; `grow_mib: 2000`
is ten times that jitter. `sustain_s: 30` is six samples, so a single spike - a tab opening, a
video seeking - is not pressure. `end_margin_mib` is wider than the trigger on purpose: an
episode that ends at the same line it started at restarts itself forever, and a 24 GB card with a
21 GB model has under 1 GB of slack to oscillate in, so the design is few, large, deliberate
evictions rather than continuous trimming. The floor is the median the telemetry recorded, the same
number the **Fits alongside** tiles use, so the trigger never goes hunting for memory that was never
there (a game's memory last evening is not a floor: it came back when the game closed).

What pressure never does: **refuse a load** - growth by a process nobody manages justifies releasing
idle workloads, never saying no to something you typed, or to a queue that asks; **chase a model you
load afterwards** - the workloads an episode is owed are decided once, at its start, and recorded;
touch a workload with work in flight, which is skipped and retried while the episode lasts; and
unload anything for a process named in `ignore:`. A workload whose `yields_to:` doesn't list
`pressure` is not a candidate either, which is how you keep one server out of it: Strata with
`yields_to: [comfy, claims]` keeps its model however big a render gets. `pressure.enabled: false`
switches the whole thing off.

**Shared GPU memory is evidence, not a trigger.** On Windows each process reports two GPU memory
figures: dedicated (what is on the card) and shared (system RAM the card borrows once it runs out -
Task Manager's "shared GPU memory"). Only dedicated is what the trigger reads. Measured on this box
in the same window: 37 processes reporting **51.0 GiB** of shared GPU memory between them, 50.9 GiB
of it Strata, while the card was merely full. Shared usage goes into the episode's row as evidence
that the card was oversubscribed at that moment, which is a different fact with a different answer.
The card-wide total is kept in the samples too (`vram_shared`), and the dashboard's VRAM card shows it
as a tile labelled **Shared (spill)** rather than a chart line: with a 21 GiB model loaded it is tens
of GiB of system RAM, so plotted against a 24 GiB card it would flatten every other series on the
chart. A backend with no such counter (Linux) says "unknown", not zero.

Both `vhotseat watch` and the dashboard's watcher run the check, and both act on it; they coordinate
through the GPU lock and the event log, the same way claim releases do, so whichever gets there
first does it and the other declines quietly. The episode is one `pressure` row - who grew, by how
much, over how long, what the card was doing, what was decided - then `pressure-release` and
`pressure-release-waiting` rows, and one `pressure-end` row when the room is back or the grower is
gone. `vhotseat status`, `vhotseat why` and the dashboard read the episode from those rows, so they
say the same thing whichever process is asked. The dashboard has a **Pressure** card for it: the
free VRAM against its threshold, the unmanaged memory against its usual floor, how long the card has
been low, and for each workload the episode was owed whether it came off, is still holding and why,
or hasn't been asked yet; while an episode is open the warning banner names the app that grew. A
workload that answers "unloaded" and keeps its VRAM is asked three times in total, then left for the
rest of the episode - that is an unload that doesn't work, not a busy moment - and the card counts
the tries from the rows, so `watch` and the dashboard aren't each allowing themselves three.

**Fits alongside.** The dashboard's policy panel answers the question the owner actually has:
can these two run at the same time? It is arithmetic on what the card has been *seen* doing:
each workload at the most the telemetry recorded it holding - a workload costs what its biggest
model costs, not what it happens to hold this second - plus what the processes vhotseat doesn't
manage *usually* hold: the median of the non-workload memory over the retention window, which is
the part of the card no handover can free. Not its peak. A game, Blender or whatever a claim rule
watches all count as non-workload memory while they run, and all of it comes back when they exit,
so a peak would have said "no room" for the next 24 hours after one evening of gaming. A pair that
fits at those numbers fits; a pair measured against what the card happens to be doing this second
is a snapshot of luck. A pair where one of the two has never been sampled holding models says so
rather than promising a fit. That median is one function, `Telemetry.floor_mib()`, and the pressure
trigger's idea of how much of the card can never be freed is the same call: two answers to that
question would disagree.

While something matches, nothing **new** loads: the gate refuses a load a request would
wake, and `switch --request` - what ComfyUI's guard node uses, because the queue is
asking on somebody's behalf - is refused too. A `switch` you type yourself is an order
and is not refused; neither is a requested switch that would load nothing, so a batch
mid-flight carries on. Nothing loaded is interrupted, and no claimed process is touched.
Built-ins know Steam (`*\steamapps\common\*`), Epic, Riot, anything under a `Games`
folder, and a handful of exes; launchers and their helpers never claim the card, because
they run all day - and neither do Wallpaper Engine and a VR runtime, which live under
`steamapps\common\` like a game does but mean nobody is playing one. Extend that list with
`claims_ignore:`; a rule of your own can still claim anything it names, non-game or not.
A process belonging to a configured workload never claims the card against its own loads
either, even when that workload is installed under `D:\Games\` and is sitting idle.
`claims_builtin: false` leaves only your rules.

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
