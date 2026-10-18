# How vhotseat works

The setup this document walks through: one GPU (16 GB in the examples), and
two services that each need almost all of it.

| Service | What it is | VRAM when loaded |
| --- | --- | --- |
| **an LLM server** (here [Strata](https://github.com/Niko1221/Strata), `:2335`) | a local LLM; an **agent** runs on it | ~14.7 GB |
| **ComfyUI** (`:8188`) | image and video generation | 7 GB (SDXL) to 13+ GB (video) |

They can't both hold the GPU. vhotseat makes them take turns, with no caller
ever having to manage the GPU:

- not the chat client that talks to the agent;
- not the agent;
- not the scripts the agent writes;
- not you in the ComfyUI web page.

This document covers:

- the pieces, and how a full agent → ComfyUI → agent flow moves the GPU;
- the race conditions and deadlocks the design has to rule out, and how it does;
- how often the GPU changes hands, what each handover costs, and the
  best-effort measures that keep that number down.

Commands, flags and config are in the [README](../README.md); writing support
for another server is in [guides/driver-authoring.md](guides/driver-authoring.md).
ComfyUI maintenance is in [comfyui-updates.md](comfyui-updates.md).

## 1. The pieces

| Piece | Role |
| --- | --- |
| `vhotseat` CLI, config `~/.config/vhotseat/config.yaml` | `status` / `switch` / `hold` / `doctor`, and `gate` for hooks. Reads state live; keeps no state file. |
| **the LLM server's pre-load hook** (Strata's `before_load` → `vhotseat gate strata`, strict) | Runs before the LLM loads its model. Decides allow / refuse; may free an idle ComfyUI first. |
| **the ComfyUI guard node** (`integrations/comfyui/vhotseat_guard` in `custom_nodes/`) | Wraps `PromptExecutor.execute_async`; runs `vhotseat switch comfy --wait 300` before **every** job. |
| `comfy-render` | Submits a batch of workflows, waits for **all** of them, and reports files or errors plus live progress. It never touches the GPU itself. |
| the `comfyui-batching` skill ([kit/](../kit/README.md)) | Tells the agent to use `comfy-render` and to batch. Best effort. |
| the agent harness's retry setting | When the LLM answers 503, the harness retries for minutes, not seconds. |
| the LLM's lazy load | The service stays up holding no VRAM until a request arrives. |

### The design in one sentence

Each side takes the GPU for itself, at the moment it needs it:

| Wants the GPU | Who makes room | Rule |
| --- | --- | --- |
| the LLM (a request from the agent, curl, its web page) | its pre-load hook | ComfyUI has jobs → **refuse** (503). ComfyUI idle but holding models → free them, then allow. ComfyUI empty → allow. |
| ComfyUI (a job from comfy-render, a script, its web page) | the guard node | Let any answer the LLM is writing finish, unload the LLM, then run the job. If that can't happen in 300 s, **fail the job visibly**. |

Two consequences follow:

- **A render is never interrupted.** While ComfyUI has anything queued or
  running, the LLM stays down.
- **An answer is never interrupted.** The guard waits for the LLM's in-flight
  request before unloading it.

Everything else, such as which mode the GPU is "in", follows from those two
rules. `vhotseat status` derives the mode from each workload's API and the
GPU's per-process memory every time; nothing records it.

## 2. A full flow

The scenario:

1. A user asks the agent, through some chat client: *"make a short video of my
   cat chasing a butterfly"*.
2. The agent plans it and renders candidate keyframes as images.
3. The agent looks at a contact sheet and picks one.
4. The agent renders the video from it.
5. The agent replies with the result.

That gives four GPU handovers: one per render round in each direction.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant Y as chat client
    participant P as agent
    participant S as LLM server
    participant H as pre-load hook
    participant L as GPU lock (flock)
    participant R as comfy-render
    participant C as ComfyUI
    participant G as guard node

    U->>Y: "make a short video of my cat…"
    Y->>P: start a turn

    Note over S,C: GPU idle: LLM lazy-unloaded, ComfyUI holding nothing

    rect rgb(232, 240, 255)
    Note over P,H: ① LLM wakes — handover 0 (cold start)
    P->>S: chat request (plan the job)
    S->>H: before_load
    H->>L: try-lock (non-blocking) → acquired
    H->>C: GET /queue → empty; GPU → ComfyUI idle
    H->>L: release
    H-->>S: allow (exit 0)
    Note over S: load model ~26 s warm / ~68 s cold
    S-->>P: "write jobs.json, then run comfy-render"
    P-->>Y: live steps
    end

    rect rgb(255, 238, 225)
    Note over P,G: ② Image round — handover 1 (LLM → ComfyUI)
    P->>R: comfy-render keyframes.json (8 seeds × 1 prompt)
    R->>C: websocket /ws (progress feed)
    R->>C: POST /prompt × 8 (all queued up front)
    C->>G: job 1 starts → execute_async (wrapped)
    G->>L: switch comfy: lock (blocking) → acquired, marked "switch comfy"
    G->>S: POST /unload?wait=300
    Note over S: no request in flight (the agent is blocked in comfy-render) → unload ~1 s
    G->>L: release
    G-->>C: run job 1
    Note over C: load SDXL, render 8 jobs back-to-back<br/>(queue never empties → LLM stays refused)
    R-->>P: "job 3/8 running: node 5 KSampler step 14/30 (47%)"
    C-->>R: 8 × completed
    R-->>P: 8 file paths, exit 0
    end

    rect rgb(225, 245, 230)
    Note over P,H: ③ agent reviews — handover 2 (ComfyUI → LLM)
    P->>S: chat request (+ tool result: 8 paths)
    S->>H: before_load
    H->>L: try-lock → acquired
    H->>C: GET /queue → empty; GPU → ComfyUI holds ~7 GB
    H->>C: POST /free (unload_models)
    H->>H: wait until ComfyUI VRAM ≤ 1500 MiB (≤ 20 s)
    opt VRAM won't drop, or the LLM's admission needs RAM ComfyUI holds
    H->>C: restart ComfyUI (~15–20 s)
    end
    H->>L: release
    H-->>S: allow
    Note over S: load ~26 s, then re-read the whole conversation<br/>(prefix cache was lost on unload)
    S-->>P: "build a contact sheet" (no GPU), picks keyframe 5, writes video.json
    end

    rect rgb(255, 238, 225)
    Note over P,G: ④ Video round — handover 3 (LLM → ComfyUI)
    P->>R: comfy-render video.json
    R->>C: POST /prompt × 1
    C->>G: execute_async → switch comfy → unload LLM
    G-->>C: run
    Note over C: load video model (~13 GB VRAM), render ~4 min
    R-->>P: "waiting to start …", then "step 12/30 (40%)" every ~10 s
    R-->>P: file path, exit 0
    end

    rect rgb(225, 245, 230)
    Note over P,H: ⑤ agent replies — handover 4 (ComfyUI → LLM)
    P->>S: chat request (+ tool result)
    S->>H: before_load → free ComfyUI (~13 GB) → allow
    Note over S: load ~26 s + re-read conversation
    S-->>P: final answer with the video
    end

    P-->>Y: reply
    Y-->>U: reply with the video attached
    Note over S,C: GPU stays with the LLM until the next ComfyUI job takes it
```

### What each actor never has to do

- **The chat client** knows nothing about the GPU. At most it turns a "GPU
  busy" 503 into a readable reply.
- **The agent** never calls `vhotseat`. It runs `comfy-render` and asks the LLM
  for its next step; the hook and the guard do the handovers.
- **comfy-render** queues and waits; the guard switches the GPU for each job it
  submits.

### Why the agent never talks to the LLM during a render

An agent is a strict loop: ask the model → run the tool → ask the model again.
While `comfy-render` runs, the agent is blocked inside that tool call, so no
LLM request can collide with the render from the agent's side. That's also why
the guard's `/unload` in step ② finds the LLM idle.

The design relies on that blocking. When it breaks, as in §3.2, the system
still stays safe, but the turn fails.

## 3. Race conditions

The safety argument has two parts:

- **The LLM never loads while ComfyUI has a job**, because its hook refuses.
- **A ComfyUI job never runs while the LLM is loaded**, because the guard
  unloads the LLM first, or fails the job.

The races below are the gaps between "checked" and "acted" in each of those.

### 3.1 A ComfyUI job is queued just after the hook checked `/queue`

The hook reads `/queue` as empty and decides to free ComfyUI and allow the
LLM. A new job lands in that gap.

Two things contain this:

- ComfyUI's `/free` only takes effect **between** jobs. With a job queued, the
  free is postponed, ComfyUI's VRAM doesn't drop, and the hook's wait for
  ≤ 1500 MiB times out after 20 s. The hook then refuses and the LLM answers
  503. The two never share the GPU.
- That new job's guard first runs `switch comfy`, which tries the LLM's
  `/unload`. The LLM isn't loaded (the load was refused), so the job runs.

If the job instead lands **after** the hook allowed the LLM, its guard does
`switch comfy --wait 300`. That waits for the LLM's in-flight request, then
unloads it. The job waits; nothing is interrupted.

An unreachable `/queue` counts as busy, not idle: ComfyUI holding models with
its API down could be mid-job, so the hook refuses.

### 3.2 The render script ends before ComfyUI's queue is empty

This is what happens without comfy-render: an agent writes its own polling
script, the script crashes on `/history`'s shape after the first of four jobs,
and the agent asks the LLM for its next step while three jobs are still
queued.

The safety rule holds: the hook refuses (correctly) because ComfyUI has jobs,
and all four images render. But if the agent harness gives up retrying after a
few seconds, the turn fails with "GPU busy".

Three fixes, from root cause outward:

1. **`comfy-render`** waits until *all* jobs have finished. It never crashes on
   a bad job, and it reports one per line. The `comfyui-batching` skill points
   the agent at it, so the agent doesn't hand-write polling loops.
2. **The harness's retry window**: an agent on a gated LLM must retry 503 (with
   `Retry-After`) for at least as long as a batch's tail, a few minutes rather
   than the usual few seconds.
3. **The chat client** turns the eventual 503 into a readable reply.

### 3.3 `switch comfy` reports "comfy" while the LLM is still loading

A lazy LLM is "unloaded" right up to the moment its engine comes up. A
`switch comfy` that trusted that would return at once, and a ComfyUI job could
start while the LLM was mid-load.

So `switch comfy` **always** calls the LLM's `/unload` when its service runs
(`release.always` in the config). `/unload` can't take the LLM's request lock
while a load holds it, so it answers "busy" and is retried (`?wait=N`) until the
load finishes, then unloads.

### 3.4 Two ComfyUI jobs and one LLM request interleave

Example: job A finishes, an LLM request arrives, then job B starts. This is
correct and safe:

- the hook frees ComfyUI and the LLM loads;
- B's guard waits for the LLM's answer and unloads it;
- B runs.

The cost is two extra handovers (~30 s each).

This happens when jobs are submitted one at a time with gaps between them,
which is exactly what `comfy-render` avoids by queuing a whole batch up front.
For a batch that must not be split even by unrelated traffic, such as another
conversation or a scheduled job, `comfy-render --hold` (or
`vhotseat hold comfy -- CMD`) keeps the lock marked `hold comfy`, and the hook
refuses every LLM load until the batch is done.

### 3.5 Something outside the system uses the GPU

The guard covers every ComfyUI job, including ones queued from ComfyUI's own
web page. The hook covers every LLM load.

Anything else that grabs VRAM isn't covered: another CUDA program, a server
started by hand outside its service, or a ComfyUI custom node that loads models
outside a job. `vhotseat status` shows that VRAM as `other`, and `doctor` warns
about it. The LLM's `admission.min_free_vram_mib` is the last line of defence:
it won't load into a GPU that lacks room. A job whose model doesn't fit fails
with ComfyUI's own out-of-memory error.

## 4. Deadlocks

Three locks are in play:

| Lock | Held by | Acquired how |
| --- | --- | --- |
| `~/.local/state/vhotseat/switch.lock` (flock; one per GPU) | `switch`, `hold`, and the hook | **blocking** in `switch` / `hold`, **non-blocking** in the hook |
| the LLM's request queue | a running request or load | blocking for requests; **non-blocking** in `/unload` |
| ComfyUI's job worker | the running job, including its guard | one job at a time |

### 4.1 `switch strata` and the hook it triggers

`switch strata` takes the lock, then calls the LLM's `/load`. That load runs
the pre-load hook, which also wants the lock.

If the hook **waited** for the lock, the two would wait on each other forever:
`switch strata` waits for `/load`, `/load` waits for the hook, and the hook
waits for the lock `switch strata` holds.

Two choices avoid it:

- The hook only **tries** the lock. If someone else holds it, the hook reads the
  file, where the holder wrote what it's doing (`switch strata`,
  `switch comfy`, `hold comfy`):
  - `switch strata` or `hold strata` → this load is the one it asked for → allow;
  - anything else → refuse.
- The hook frees ComfyUI **itself** rather than calling `switch strata`. Calling
  it would end in the LLM's `/load` again, which would wait on the load that is
  running this very hook.

### 4.2 `hold` inside `hold`, `switch` inside `hold`

A render script run under `hold comfy` might call `vhotseat switch comfy` or
another `hold`. Taking the lock again from inside would wait forever on the
outer hold.

`hold` sets `VHOTSEAT_HOLD=comfy` in COMMAND's environment:

| Called inside a hold | Behaviour |
| --- | --- |
| `switch comfy` | no-op |
| `switch strata` | fails with a clear message |
| `hold comfy` | runs its command directly |

The guard runs in ComfyUI's process, without that variable. So a
`switch comfy` from outside the hold's command, while a `hold comfy` owns the
lock, is also a no-op: the GPU is already ComfyUI's. Otherwise a job queued by
the held script would wait on the hold, which waits on the job.

### 4.3 The guard, the hook and the LLM's request lock

Could the guard and the hook end up waiting on each other? The setup:

- **The hook** runs inside the LLM's load, so it holds the LLM's request lock,
  and it holds the GPU lock while it frees ComfyUI (up to 20 s).
- **The guard** runs inside ComfyUI's job worker: `switch comfy`, which waits
  for the GPU lock, then calls `/unload`, which needs the request lock.

There is no cycle:

- `/unload` takes the request lock **non-blocking**. It answers "busy" and is
  retried, so the guard never waits on it while holding the GPU lock.
- The hook never waits on ComfyUI's worker. It only waits for ComfyUI's VRAM to
  drop, which is bounded at 20 s.

When both happen at once (a job starting just as the LLM wakes), the timing
goes:

1. The hook holds the lock.
2. The guard waits for it.
3. Either the hook's free succeeds and it allows the load, or ComfyUI's new job
   keeps its VRAM up, the hook times out and refuses.
4. The hook releases the lock.
5. The guard unloads the LLM if it loaded, and the job runs.

This is a **bounded wait**, not a deadlock.

A `switch` with `--wait` doesn't hold the lock while it waits for another
workload's running work: it waits first, then takes the lock and checks again.
Otherwise a `switch strata --wait 600` during a batch would stall every guard
for the whole wait.

### 4.4 Every wait has a limit

| Waiter | Bound | On expiry |
| --- | --- | --- |
| hook: ComfyUI's VRAM to drop | 20 s | refuse → the LLM answers 503 |
| the LLM: the hook itself | its hook timeout (`gate.timeout`, e.g. 90 s) | strict → 503 |
| guard: `switch comfy --wait` | 300 s (`VHOTSEAT_GUARD_WAIT_S`), +120 s subprocess cap | job fails visibly: "GPU busy, job not run" |
| `switch strata`: the LLM's `/load` | `max(load.timeout, --wait)` s | switch fails, exit 1 |
| comfy-render: no progress | 1800 s without a job finishing, a node or step starting, or the queue changing (`--timeout`); the batch as a whole is unbounded | exit 3, jobs left queued |
| the agent harness: LLM 503s | its retry window | the turn fails with "GPU busy" |

The one unbounded wait is a `switch` or `hold` queuing behind another `hold`.
That's deliberate: a hold is a promise not to give the GPU up until its
command exits.

## 5. Exchange frequency

### 5.1 What one handover costs

Example figures, measured on an RTX 5070 Ti with a ~35 GB MoE LLM (Strata,
IQ2_XS):

| Direction | Step | Time |
| --- | --- | --- |
| LLM → ComfyUI | guard: the LLM's `/unload` | ~1 s |
| | ComfyUI loads its model | SDXL ~10–25 s, video longer |
| ComfyUI → LLM | hook: ComfyUI `/free` | ~2 s |
| | hook: ComfyUI restart, only if its VRAM won't drop or the LLM's admission needs its RAM | ~15–20 s |
| | the LLM loads, warm (files in page cache) | ~26 s |
| | the LLM loads, cold (read from disk) | ~68 s |
| | **the LLM re-reads the conversation** (prefix cache lost on unload) | ~1 min per 30K tokens |

A full round trip, LLM → ComfyUI → LLM, costs **≥ 1 minute of overhead**
before any rendering, more as the conversation grows. The re-read is the cost
that keeps growing: in a long conversation it's the largest single item.

### 5.2 How many handovers a task takes

A task with *k* render rounds needs:

```
handovers = 2k              (one each way per round)
overhead  ≈ k × (unload + ComfyUI load + /free + LLM load + re-read)
```

The flow above has k = 2 (images, then video): 4 handovers plus the cold
start. Two things multiply this:

| Pattern | Handovers for N jobs | Why |
| --- | --- | --- |
| jobs submitted **one at a time**, with an LLM request between them | up to 2N | each gap lets the LLM take the GPU back |
| jobs submitted **as a batch**, one `comfy-render` call | 2 | ComfyUI's queue never empties, so the hook refuses the LLM throughout |
| re-rolling: render, look, render again | 2 per extra round | each look needs the LLM |

A four-panel comic where one panel takes three tries ("waffles", then "raw
steak", then right) is 6 handovers for that panel alone: about 3 minutes of
pure overhead, growing with each re-read.

### 5.3 Best-effort measures that keep it down

None of these are enforced; correctness never depends on them. They lower the
number and size of handovers.

| Measure | Where | Effect |
| --- | --- | --- |
| **Batch per round:** queue every job, then wait once | `comfy-render`, batching skill | N jobs → 2 handovers, not 2N |
| **Over-generate instead of re-rolling:** 6–8 seeds plus a few prompt variants for a risky shot | batching skill | turns extra rounds (2 handovers each) into extra jobs inside one round (~0 extra) |
| **Plan all prompts before the first render** | batching skill | one batch covers the whole task, instead of one per panel |
| **One contact sheet per round** | batching skill | one image for the LLM to read, not N |
| **Non-GPU work outside the batch:** layout, captions, file moves | batching skill | doesn't put an LLM turn between two ComfyUI jobs |
| **Short tool output:** one summary line, throttled progress | `comfy-render`, batching skill | less text to re-read after each reload |
| **`--hold` for batches that must not be split** | `comfy-render --hold`, `vhotseat hold` | stops unrelated traffic (another chat, a scheduled job) from forcing two extra handovers mid-batch |
| **Lazy LLM, no idle unload** | the LLM's config | the LLM unloads only when ComfyUI actually needs the GPU, never on a timer |
| **Free ComfyUI only when the LLM needs it** | hook | ComfyUI keeps its model between jobs, so back-to-back renders skip the reload |

### 5.4 What's not reduced yet

- **The re-read.** Some LLM servers can save and restore their prefix cache to
  disk. Saving before `/unload` and restoring after `/load` would replace the
  ~1 min/30K-token re-read with a file read. Not built; it needs checking that a
  restored cache is actually reused across an engine restart.
- **Waiting while blocked.** During a render the agent's turn blocks inside a
  tool call. An async variant would let the chat client end the turn while the
  GPU job runs, then wake the agent when it's done. That changes nothing about
  handover count; it frees the conversation and removes the dependence on the
  harness's retry window.
- **Request-level contention.** An LLM server that serves one request at a time
  makes two conversations queue behind each other. That's unrelated to the GPU
  handover, but it adds latency on top of it.

## 6. Observing it

- `vhotseat status` shows who owns the GPU, what each workload holds (VRAM,
  RAM, swap), and the last event.
- `~/.local/state/vhotseat/events.jsonl` keeps the last 500 decisions: every
  `switch`, every `hold` / `hold-end`, and every gate decision with its `source`
  (`hook` = during a switch or hold, `request` = a request that woke the
  workload) and `reason`. For example:

  ```
  strata-load  source=request allowed=true  reason="ComfyUI idle; freed its 7044 MiB of models"
  strata-load  source=request allowed=false reason="ComfyUI has 3 job(s) running or queued"
  switch       to=comfy ok=true previous=strata
  ```

  A run of `allowed=false` lines followed by a "GPU busy" reply from the agent
  is the signature of §3.2.
- **In ComfyUI's UI and `/history`:** a guard refusal shows as an
  `execution_error` "GPU busy, job not run: …".
- **In the chat:** comfy-render's progress lines (`waiting to start …`, then
  `step N/M`) show as live tool output while the agent waits.
