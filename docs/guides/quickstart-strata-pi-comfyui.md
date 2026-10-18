# Quick start: Strata + pi + ComfyUI on one 16 GB card

This sets up the configuration vhotseat was built and tested on: a local LLM
agent that drives ComfyUI video workflows on the same consumer GPU, unattended.

| Part | What was tested |
| --- | --- |
| GPU | NVIDIA RTX 5070 Ti, 16 GB VRAM |
| RAM | 64 GB |
| OS | Ubuntu (systemd user services) |
| LLM server | [Strata](https://github.com/Niko1221/Strata), serving [Qwen3.8-Flash-Next](https://huggingface.co/Qwen/Qwen3.8-Flash-Next) at IQ3_XXS, 128K context |
| Agent | [pi](https://github.com/earendil-works/pi) 1.3 (`@earendil-works/pi-coding-agent`) |
| Image/video | [ComfyUI](https://github.com/comfyanonymous/ComfyUI) 0.39 with [MiniMax H3](https://huggingface.co/Comfy-Org/MiniMax-H3) (int8 `fl2va` and `ref2va` models) |

Any part can be swapped (another agent, another ComfyUI model, IQ2_XS instead of
IQ3_XXS), but the figures below are for this set.

**For a coding agent following this guide:** do the steps in order and run each
step's check before going on. Ask the user before changing anything outside
the folders named here, and back up every config file you edit
(`cp X X.pre-vhotseat`). Paths assume `~/Strata` and `~/ComfyUI`; use the user's
own if they differ, and the same paths everywhere.

## Why the pieces need coordinating

Strata keeps Qwen's dense weights in ~14.7 GB of VRAM and its experts in RAM:
IQ3_XXS needs about 46 GB of *available* RAM to load. A MiniMax H3 render needs
most of the card, and ComfyUI keeps 6–15 GB of RAM after a video render even
once its models are freed. So neither fits on top of the other, in VRAM or RAM.
vhotseat makes each one ask before it loads, releases the other (never in the
middle of an answer or a render), and restarts an idle ComfyUI when its RAM is
what's in the way.

## 1. Strata

Install it with its own setup (it downloads ~47 GB for IQ3_XXS):

```sh
git clone https://github.com/Niko1221/Strata ~/Strata
cd ~/Strata && ./setup.sh --yes --family qwen --model IQ3_XXS --vision yes --no-start
```

Setup writes `strata-iq3_xxs.json`. Add these keys to it:

```json
"lazy_load": true,
"min_free_vram_mib": 14000,
"before_load": ["vhotseat", "gate", "strata"],
"before_load_strict": true,
"before_load_timeout_s": 90
```

- `lazy_load`: start without loading; the first request loads the model.
- `before_load`: every load asks vhotseat first. `strict` turns a refusal into
  a 503 with `Retry-After`, never a load on top of ComfyUI.
- If `vhotseat` isn't on the PATH of the service, use its full path
  (`~/.local/bin/vhotseat`, written out in full).

Run it as a systemd user service, `~/.config/systemd/user/strata.service`:

```ini
[Unit]
Description=Strata on 127.0.0.1:2335
After=network.target

[Service]
WorkingDirectory=%h/Strata
ExecStart=%h/Strata/.venv/bin/python %h/Strata/serve/server.py --engine strata --config %h/Strata/strata-iq3_xxs.json --port 2335 --host 127.0.0.1
Restart=on-failure
RestartSec=10
TimeoutStopSec=60

[Install]
WantedBy=default.target
```

Don't enable it yet (step 4 needs vhotseat installed first).

**Check:** `ls ~/Strata/strata-iq3_xxs.json` and
`python3 -m json.tool ~/Strata/strata-iq3_xxs.json >/dev/null`.

## 2. ComfyUI and MiniMax H3

Install ComfyUI in `~/ComfyUI` with its own virtualenv (see its README), then
download the MiniMax H3 files from
[Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3):

| Folder under `~/ComfyUI/models/` | File | Size |
| --- | --- | --- |
| `diffusion_models/` | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` (text/image to video) | 19.5 GB |
| `diffusion_models/` | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` (reference to video; optional) | 19.5 GB |
| `text_encoders/` | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 14.6 GB |
| `vae/` | `minimax_h3_video_vae_int8_convrot.safetensors` | 2.6 GB |
| `vae/` | `minimax_h3_audio_vae_fp32.safetensors` | 0.6 GB |
| `loras/` | a turbo LoRA from the same repo (e.g. `minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors`) | 1.8 GB |

ComfyUI's built-in MiniMax H3 templates (Workflow → Browse Templates → Video)
use exactly these names; open one and export it with **Export (API)** to get a
workflow the agent can start from.

Run it as `~/.config/systemd/user/comfyui.service`:

```ini
[Unit]
Description=ComfyUI
After=network-online.target

[Service]
WorkingDirectory=%h/ComfyUI
ExecStart=%h/ComfyUI/.venv/bin/python main.py --listen 127.0.0.1 --port 8188
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

The unit matters: vhotseat attributes VRAM to a workload by the systemd unit
its process runs in, and restarts ComfyUI through it.

**Check:** `systemctl --user daemon-reload && systemctl --user start comfyui`,
then `curl -s 127.0.0.1:8188/queue` answers JSON.

## 3. vhotseat

```sh
uv tool install vhotseat          # or: pipx install vhotseat
mkdir -p ~/.config/vhotseat
curl -fsSL https://raw.githubusercontent.com/wandermyz/vhotseat/main/config.example.yaml \
  -o ~/.config/vhotseat/config.yaml
```

The example config *is* this setup: `strata` on `:2335`, `comfy` on `:8188`,
admission figures for IQ3_XXS and IQ2_XS. Edit only the two paths if yours
differ: `workloads.strata.gate.config` (the Strata JSON from step 1) and
`workloads.comfy.gate.custom_nodes`.

Install the ComfyUI guard, the node that makes every job ask vhotseat first:

```sh
comfy node install vhotseat-guard
# or from a checkout of this repo:
ln -s "$PWD/integrations/comfyui/vhotseat_guard" ~/ComfyUI/custom_nodes/
systemctl --user restart comfyui
```

**Check:** `journalctl --user -u comfyui -n 50 | grep vhotseat_guard` shows
`vhotseat_guard: on`.

## 4. Start Strata and check the handover

```sh
systemctl --user daemon-reload
systemctl --user enable --now strata comfyui
vhotseat doctor
```

Every line must be `ok`. Then, with nothing else using the GPU:

```sh
vhotseat doctor --exercise strata   # load ~30 s, release ~1 s
vhotseat doctor --exercise comfy
vhotseat status
```

## 5. pi

```sh
npm install -g @earendil-works/pi-coding-agent
```

Point pi at Strata in `~/.pi/agent/models.json`:

```json
{
  "providers": {
    "strata": {
      "baseUrl": "http://127.0.0.1:2335/v1",
      "api": "openai-completions",
      "apiKey": "none",
      "models": [{
        "id": "qwen3.8-flash-next-iq3_xxs",
        "name": "Qwen3.8-Flash-Next IQ3_XXS (Strata)",
        "reasoning": true,
        "input": ["text", "image"],
        "contextWindow": 131072,
        "maxTokens": 32768,
        "compat": {"supportsDeveloperRole": false, "maxTokensField": "max_tokens"}
      }]
    }
  }
}
```

And in `~/.pi/agent/settings.json`:

```json
"defaultProvider": "strata",
"defaultModel": "qwen3.8-flash-next-iq3_xxs",
"retry": {"maxRetries": 8, "baseDelayMs": 2000, "maxAgentDelayMs": 30000}
```

The retry window matters. While ComfyUI still has jobs, Strata answers 503 and
pi must wait them out: these settings retry for about 150 s (2, 4, 8, 16, then
30 s ×4). With a shorter window, a turn that continues while the last render
finishes fails with "GPU busy".

Give pi the batching skill, in the folder you'll run pi from:

```sh
mkdir -p .pi/skills
ln -s <vhotseat checkout>/kit/skills/comfyui-batching .pi/skills/
```

It tells pi to run workflows through `comfy-render` (installed with vhotseat)
in as few batches as possible, instead of hand-written polling scripts. Each
handover costs a ~30 s reload plus a re-read of the conversation, so batching is
what makes an overnight session practical.

**Check:** `pi -p "Say OK."` answers (the first request loads the model, ~30 s).

## 6. End-to-end test

Ask pi, from that folder, for something that needs ComfyUI and then needs it
to look at the result, e.g. *"Make two 3-second clips of a cat on a windowsill
with MiniMax H3, then tell me which one looks better."* Watch:

```sh
journalctl --user -u comfyui -f | grep -E "vhotseat|Prompt executed"
tail -f ~/.local/state/vhotseat/events.jsonl
```

You should see: Strata released when the batch starts (`switch` to `comfy`),
both jobs reported by `comfy-render`, then a `strata-load` with
`ComfyUI idle; freed …` when pi continues, and pi's answer about the clips.
`vhotseat status` at the end shows `mode: strata`.

## If something goes wrong

| Symptom | Cause |
| --- | --- |
| pi: "the GPU is busy with ComfyUI jobs" | ComfyUI still had jobs when pi's retries ran out. Raise `retry` in pi's settings, or wait and continue. |
| ComfyUI job: `GPU busy, job not run: …` | Strata was answering for longer than the guard waits (`VHOTSEAT_GUARD_WAIT_S`, default 300 s). Re-run the job. |
| Strata 503 with `needs 46000 MiB of available RAM` | Something outside vhotseat holds RAM. Close it, or use IQ2_XS (40 GB). |
| `doctor` warns about VRAM held by other processes | A process outside both units is on the GPU (a desktop app, a stray server). |

More: [how it works](../how-it-works.md), and the
[setup skill](../../kit/skills/vhotseat-setup/SKILL.md) for other servers.
