---
name: vhotseat-setup
description: Install and set up vhotseat on a machine where several GPU workloads (a local LLM server such as Strata, ComfyUI, others) share one NVIDIA GPU, so they take turns instead of loading on top of each other. Use when the user asks to "set up vhotseat", "install vhotseat", or make their LLM and ComfyUI share the GPU.
---

# Setting up vhotseat

vhotseat hands one GPU between workloads: each one asks before it loads, and
the others are released first, never in the middle of their work. Setting it
up means: install it, describe the workloads in `~/.config/vhotseat/config.yaml`,
install a **gate** in each workload (the thing that asks vhotseat before a
load), then check.

The docs are the source of truth; this is the order to work in. In the
vhotseat repo: `config.example.yaml` (every config key, commented),
`docs/how-it-works.md`, `docs/guides/proxy-gate.md`.

## Rules

- **Ask before changing anything outside vhotseat**: a server's config, its
  port, a systemd unit, someone else's code. Show the diff first.
- **Back up every config you edit** (`cp X X.pre-vhotseat`).
- **Don't report success without step 6.**
- Do it while the GPU is idle: no render running, no LLM answering.
- Linux with an NVIDIA GPU only, for now.

## 1. Install

```sh
uv tool install vhotseat      # or: pipx install vhotseat
vhotseat --help
```

This installs two commands: `vhotseat` and `comfy-render`. Both must be on the
`PATH` of the services that will call them (a systemd user unit usually sees
`~/.local/bin`); if not, use the absolute path in each gate below.

## 2. Find the GPU workloads

Work out what uses the GPU, how each runs, and where it listens:

```sh
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
ss -ltnp                                   # listening ports and their processes
systemctl --user list-units --type=service --state=running
systemctl list-units --type=service --state=running
ps -o pid,unit,cmd -p <PID>                # which unit a GPU process belongs to
```

For each one, note: its name, its URL, its systemd unit (user or system), and
which of the cases below it is. Tell the user what you found and agree on the
list before writing config. Something that only runs now and then (a training
script) doesn't need a workload: tell the user to run it as
`vhotseat hold NAME -- COMMAND`, which still needs a workload entry for NAME.

| Workload | What to do |
| --- | --- |
| Strata | config only: copy the `strata` block of `config.example.yaml` (step 3) and its hook (step 4) |
| ComfyUI | config only: copy the `comfy` block, then install the guard node (step 4) |
| Anything else | hand off to the **vhotseat-add-workload** skill for that one, after steps 3–4 for the others |

## 3. Write the config

Start from `config.example.yaml` and keep only the workloads found in step 2:

```sh
mkdir -p ~/.config/vhotseat
cp config.example.yaml ~/.config/vhotseat/config.yaml   # from the repo
```

Then set, for each workload: `url`, `service` (`{systemd-user: UNIT}` or
`{systemd: UNIT}`), and the gate's paths (`gate.config` for Strata,
`gate.custom_nodes` for ComfyUI) so `doctor` can check them. Measure
`admission` for an LLM rather than copying the example's numbers: from idle,
load it (`vhotseat switch NAME`), note how far `MemAvailable` dropped
(`grep MemAvailable /proc/meminfo` before and after) and its VRAM in
`vhotseat status`, add ~1.5 GiB. Leave `patches: []` unless step 5 says
otherwise.

**Check:** `vhotseat status` shows each workload with the right state, and
`vhotseat doctor` has no FAIL other than gates not installed yet.

## 4. Install the gates

**Strata** (hook gate). In Strata's JSON config (the file named in
`gate.config`), set:

```json
"before_load": ["vhotseat", "gate", "strata"],
"before_load_strict": true,
"before_load_timeout_s": 90
```

`before_load_strict` makes a refused gate a 503 instead of a load anyway.
`before_load_timeout_s` must be at least the workload's `gate.timeout`. Leave
Strata's idle unload off: vhotseat decides when it unloads. Restart Strata.

**ComfyUI** (plugin gate). Install the guard node from the latest GitHub
release, or symlink it from a checkout of the repo:

```sh
curl -fsSL https://github.com/wandermyz/vhotseat/releases/latest/download/vhotseat_guard.zip -o /tmp/vhotseat_guard.zip
unzip -o /tmp/vhotseat_guard.zip -d <ComfyUI>/custom_nodes/
# or
ln -s /path/to/vhotseat/integrations/comfyui/vhotseat_guard <ComfyUI>/custom_nodes/vhotseat_guard
```

Not from the ComfyUI Registry for now: its listing isn't live yet.

Restart ComfyUI. Its log should show `vhotseat_guard: on (…, wait 300s)`. If
`vhotseat` isn't on ComfyUI's PATH, set `VHOTSEAT_BIN` in its unit's
environment.

**Anything else:** the vhotseat-add-workload skill.

## 5. Local patches

If a workload's release leaves VRAM behind (the **vhotseat-vram-leak** skill
diagnoses that) and you patch it, record the patch under `patches:` so
`doctor` notices when an update drops it.

## 6. Verify

All of these, and report each result to the user:

1. `vhotseat doctor`: no FAIL, and no warning about VRAM held by processes no
   workload owns.
2. `vhotseat doctor --exercise NAME` for each workload: passes. (It loads and
   unloads for real; tell the user first.)
3. Take turns: with the LLM loaded, queue a ComfyUI job (`comfy-render` with a
   small workflow). The LLM is unloaded first and the job succeeds. Then send
   the LLM a request: ComfyUI is released and the LLM answers.
4. Never interrupt: while the LLM writes a long answer, queue a ComfyUI job.
   The answer completes; then the job runs.

## 7. Offer the agent skills

Ask whether to install the kit's skills for the user's agents, by copying or
symlinking each folder of `kit/skills/` into the agent's skills folder:
`~/.claude/skills/` (Claude Code), `~/.codex/skills/` (Codex),
`~/.pi/agent/skills/` (pi). The agent that runs on the local LLM should get
**comfyui-batching**; the coding agent gets the `vhotseat-*` skills.

## 8. Keep the dashboard up

History only accumulates while something samples, so ask whether to register the
dashboard at logon: `vhotseat autostart install --command web` (Task Scheduler on
Windows, a systemd user unit elsewhere; `vhotseat autostart status` to check, and
`remove` to undo). On Windows a logon trigger needs elevation: without it it falls
back to the per-user Run key through a hidden launcher, which is the same thing for
one user and needs no console window. Then open the dashboard once and point the
user at `http://localhost:8765/`.
