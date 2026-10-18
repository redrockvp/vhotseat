---
name: vhotseat-add-workload
description: Add a new GPU workload (an LLM server, an image or video generator, a training job) to vhotseat, so it takes turns on the GPU with the others instead of loading on top of them. Use when the user asks to add, support, integrate or set up a server or tool "with vhotseat", "on the shared GPU", or "so it doesn't fight ComfyUI / the LLM for the GPU".
---

# Adding a workload to vhotseat

vhotseat hands one GPU between workloads: each one asks before it loads, and
the others are released first, never in the middle of their work. A workload
needs two things: a **driver** (how vhotseat asks it "are you loaded? are you
busy?" and tells it "drop your VRAM") and a **gate** (something that runs
before it loads and asks vhotseat for the GPU).

The docs are the source of truth; this is the order to work in. Find them in
the vhotseat repo: `config.example.yaml` (every config key, commented),
`docs/how-it-works.md`, `docs/guides/proxy-gate.md`.

## Rules

- **Ask before changing anything outside vhotseat**: a server's config, its
  port, a systemd unit, someone else's code. Show the diff first.
- **Back up every config you edit** (`cp X X.pre-vhotseat`).
- **Don't report success without step 6.**
- Run everything while the GPU is idle (`vhotseat status`: nothing running).

## 1. Learn the server

From its docs and by trying it, answer each of these and write the answers down
(you'll need them in step 3):

1. What loads a model? (a request, a CLI command, starting the process)
2. How do you know a model is loaded? (an endpoint, a field, or only `nvidia-smi`)
3. How do you know it's working on something? (a queue, an in-flight count, or nothing)
4. How do you unload it without killing work? If there's no way, can it be
   restarted safely when idle?
5. Does it unload on its own after a while? (make that "never": vhotseat decides)
6. How does it run? (systemd unit, docker, a terminal)

**Check:** you loaded a model, saw it in your answer to 2 and in `nvidia-smi`,
unloaded it per 4, and saw the VRAM drop.

## 2. Pick the gate

| The server… | Gate |
| --- | --- |
| can run a command before it loads a model | **hook**: its pre-load command runs `vhotseat gate NAME`; non-zero exit = don't load |
| has a plugin system that runs before each job | **plugin**: a plugin that runs `vhotseat switch NAME --wait 300` and fails the job if that fails |
| is only reached over HTTP | **proxy**: follow `docs/guides/proxy-gate.md` to build one |
| isn't a server | **hold**: always run it as `vhotseat hold NAME -- COMMAND` |

Take the first row that applies. **Check:** you can say why the rows above it don't.

## 3. Write the config

Add the workload to `~/.config/vhotseat/config.yaml`, copying the shape of the
closest example in `config.example.yaml`:

- An HTTP API that answers questions 1–4: `driver: http`, with `resident:`,
  `release:` (and `busy:`, `load:`, `model:` if it has them). Strata is the
  model to copy.
- Too much logic for config (several requests, a body with data from another
  response): a Python driver, a subclass of `vhotseat.drivers.base.Workload`
  like `vhotseat.drivers.comfyui.ComfyUI`, and ask the user before adding code.

Then:

- `admission:`: how much available RAM and free VRAM it needs to load. Measure
  it: from idle, `vhotseat switch NAME` (or load it however it loads), and note
  how far `MemAvailable` dropped (`grep MemAvailable /proc/meminfo` before and
  after) and its VRAM in `vhotseat status`. Add ~1.5 GiB. If it serves several
  models of different sizes, `admission.by_model`.
- `allow_restart: true` if a restart is safe when it's idle and unload isn't
  always enough.

**Check:** `vhotseat doctor` reports no FAIL, and `vhotseat status` shows the
workload correctly as you load and unload it by hand.

## 4. Install the gate

Per step 2, then restart the server so it picks it up (most read their config at
startup). **Check:** the server's log shows the gate running on the next load,
or for a proxy, its `/_vhotseat/status` answers.

## 5. Local fixes

If you had to patch the server or one of its plugins to make unloading work,
mark the change with a comment containing `LOCAL PATCH (vhotseat)`, save the
diff next to the config, and add an entry under `patches:` so `vhotseat doctor`
notices when an update drops it. Consider sending the fix upstream.

## 6. Verify

All of these, and report each result to the user:

1. `vhotseat doctor`: no FAIL.
2. `vhotseat doctor --exercise NAME`: passes (it loads and unloads for real).
3. Take turns: from another workload holding the GPU, make this one load the
   way its users do (a request, a job). The other is released first; this one
   loads; `vhotseat status` shows it.
4. Never interrupt: while this one is working (a long request, a running job),
   `vhotseat switch OTHER --wait 300` waits for the work to finish before
   unloading it.
5. The other way round: while another workload is working, a load of this one
   waits or is refused; it never loads on top.
