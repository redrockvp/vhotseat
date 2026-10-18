# Agent kit

vhotseat's core makes taking turns on the GPU safe. The kit makes it cheap
for an agent to use, and lets a coding agent set it up and keep it working.
Nothing in the core depends on it.

## comfy-render

Installed with vhotseat (`pip install vhotseat` gives both commands). It
queues a whole batch of ComfyUI workflows at once, waits for every job, and
prints one line per job: the files it saved, or ComfyUI's own error. While
ComfyUI's queue is busy the LLM can't load, so a batch costs two handovers
instead of two per job; `--hold` keeps the GPU on ComfyUI between jobs too.
See `comfy-render --help`.

## Skills

In the [Agent Skills](https://agentskills.io) `SKILL.md` format, read by Claude
Code, Codex, pi and others.

| Skill | For | What it does |
| --- | --- | --- |
| [comfyui-batching](skills/comfyui-batching/SKILL.md) | the agent running on the local LLM | Run ComfyUI work through `comfy-render`, in as few batches as possible |
| [vhotseat-setup](skills/vhotseat-setup/SKILL.md) | your coding agent | Install vhotseat, find the GPU workloads, write the config, install the gates, check |
| [vhotseat-add-workload](skills/vhotseat-add-workload/SKILL.md) | your coding agent | Add a server vhotseat doesn't know yet: driver, gate, admission, checks |
| [vhotseat-comfyui-update](skills/vhotseat-comfyui-update/SKILL.md) | your coding agent | After a ComfyUI update: check the guard still works, roll back or fix it |
| [vhotseat-vram-leak](skills/vhotseat-vram-leak/SKILL.md) | your coding agent | A release that leaves VRAM behind: find it, fix it, record the patch |

Install a skill by copying or symlinking its folder into your agent's skills
folder:

```sh
ln -s "$PWD/kit/skills/vhotseat-setup" ~/.claude/skills/      # Claude Code
ln -s "$PWD/kit/skills/vhotseat-setup" ~/.codex/skills/       # Codex
ln -s "$PWD/kit/skills/comfyui-batching" ~/.pi/agent/skills/  # pi
```

The `vhotseat-setup` skill offers to do this for you.
