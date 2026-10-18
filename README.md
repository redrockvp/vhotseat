# vhotseat

[![CI](https://github.com/wandermyz/vhotseat/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/wandermyz/vhotseat/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/wandermyz/vhotseat?sort=semver)](https://github.com/wandermyz/vhotseat/releases/latest)

**Want your local LLM agent to drive ComfyUI on the same consumer graphics
card?** Without coordination, the first video render lands on top of the
loaded LLM, and something runs out of memory. **Boom, OOM!**

vhotseat makes [Strata](https://github.com/Niko1221/Strata) and
[ComfyUI](https://github.com/comfyanonymous/ComfyUI) take turns on VRAM *and*
RAM: when the agent queues a render, the LLM is unloaded; when it asks the LLM
its next question, ComfyUI is freed. Neither ever loads on top of the other,
and **running work is never interrupted**: not an answer the LLM is writing,
not a render in progress.

> v-hotseat: VRAM hotseat. Hot-seat multiplayer: one device, players take
> turns, nobody is kicked out mid-turn.

![vhotseat: one GPU, local agentic GenAI. pi drives, ComfyUI renders, Strata reviews and composites.](https://raw.githubusercontent.com/wandermyz/vhotseat/a619e2e66e284521f3a8f66e2892790a562403df/branding/social-preview.png)

**Tested on:**

| Part | Tested with |
| --- | --- |
| GPU / RAM | RTX 5070 Ti (16 GB VRAM), 64 GB RAM |
| LLM | Strata serving Qwen3.8-Flash-Next IQ3_XXS (128K context) |
| Agent | [pi](https://github.com/earendil-works/pi) |
| ComfyUI | MiniMax H3 video (int8 `fl2va` / `ref2va`) |

With this setup, pi drove long-running video workflows unattended, over
several rounds of render → review → render, overnight.

Status: Linux, NVIDIA. Windows is designed for and planned. MIT licence.

**→ [Quick start for this exact setup](docs/guides/quickstart-strata-pi-comfyui.md)**:
a step-by-step guide that you, or your coding agent, can follow. Or just
prompt your coding agent to set up the whole workflow:

> Set up vhotseat so my local LLM agent can drive ComfyUI on this GPU without
> running out of memory. Follow
> https://github.com/wandermyz/vhotseat/blob/main/docs/guides/quickstart-strata-pi-comfyui.md,
> adapting it to the servers and paths on this machine, and run each step's
> check before going on.

## How it works

The servers stay as they are, and every client is covered: the agent, its
scripts, a web UI, curl. Nobody calls vhotseat; each workload asks for the GPU
through a **gate** right before it loads, and vhotseat makes room by releasing
the others:

- **The LLM** runs `vhotseat gate` from its pre-load hook. While ComfyUI has
  work queued the load is refused (a 503 the agent's harness retries).
- **ComfyUI** runs `vhotseat switch` before every job through a small custom
  node, the guard. It waits for any answer the LLM is writing, then unloads it.
- **Admission** checks there's enough free VRAM and RAM before a load, and
  restarts an idle ComfyUI if its leftover RAM is what's in the way.
- **[comfy-render](kit/README.md#comfy-render)** lets the agent run a batch of
  workflows in one call, so a batch costs two handovers instead of two per job.

Strata and ComfyUI are just the built-in examples: anything that uses the GPU
(another LLM server, a TTS server, a training run) can join through a config
entry or a small driver, with a hook, plugin, proxy or `hold` as its gate.

**How it differs.** [llama-swap](https://github.com/mostlygeek/llama-swap) and
Ollama's scheduler swap models between LLM servers they control; they don't
take turns with a non-LLM app like ComfyUI, and don't wait for a running job.
vhotseat arbitrates between *different* servers, of any kind, and can sit on
top of either (they can be one of its workloads).

## Install

```sh
uv tool install vhotseat      # or: pipx install vhotseat
```

That installs two commands, `vhotseat` and `comfy-render`, with one dependency
(PyYAML). From a checkout, `./vhotseat` and `./comfy-render` run as they are;
symlink them into `~/.local/bin`.

Then set it up for your servers: follow the
[quick start](docs/guides/quickstart-strata-pi-comfyui.md), or let a coding
agent do it with the [vhotseat-setup skill](kit/skills/vhotseat-setup/SKILL.md).
[config.example.yaml](config.example.yaml) is commented key by key, and
[examples/](examples/) has configs for other servers.

```sh
vhotseat doctor     # check the config, the GPU, every workload and gate
vhotseat status     # who owns the GPU and what each workload holds
vhotseat web        # dashboard: RAM and VRAM over time, handovers, events, logs
```

![The dashboard on an RTX 5070 Ti: Strata hands the GPU to ComfyUI at 01:04, and a render is running.](https://raw.githubusercontent.com/wandermyz/vhotseat/24999a8e62e3a6342e9af6c3bc70042e77765c60/web-dashboard/desktop.png)

## Documentation

- [Quick start](docs/guides/quickstart-strata-pi-comfyui.md): Strata + pi +
  ComfyUI on one 16 GB card, step by step.
- [How it works](docs/how-it-works.md): a full agent → ComfyUI → agent flow,
  the races and deadlocks, and how often the GPU changes hands.
- [Reference](docs/reference.md): commands, gates, admission, the ComfyUI
  guard, `comfy-render`, the dashboard and telemetry.
- [Agent kit](kit/README.md): `comfy-render` and
  [Agent Skills](https://agentskills.io) for the agent that renders, and for
  the coding agent that sets vhotseat up and keeps it working.
- [Writing a driver](docs/guides/driver-authoring.md) for another server, and
  [building a proxy gate](docs/guides/proxy-gate.md) for one with no pre-load
  hook.
- [After a ComfyUI update](docs/comfyui-updates.md): checking the guard still
  works. [Fixing a VRAM leak](docs/examples/vram-leak-tts-audio-suite.md) in a
  custom node is a worked example.

## Development

```sh
uv run --group dev pytest       # ~2 min; fake LLM and ComfyUI servers, no GPU needed
uvx ruff check .
```

The tests run the real CLI and arbiter against fake servers over HTTP on
localhost; the config they use is [config.example.yaml](config.example.yaml),
and every file in [examples/](examples/) is validated. CI runs both on Python
3.10 to 3.13.

## Licence

MIT. See [LICENSE](LICENSE).
