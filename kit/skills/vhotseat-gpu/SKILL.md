---
name: vhotseat-gpu
description: Load for any GPU work, ComfyUI renders, model loads, holds, and bookings.
---

# GPU work

Use `comfy-render` for ComfyUI; never POST directly to `/prompt`. Put at most four jobs in each batch. See the sibling [comfyui-batching](../comfyui-batching/SKILL.md) skill for workflow and review details; follow the rules here where they differ.

```sh
comfy-render --hold comfy --hold-wait 900 --timeout 1800 --json jobs.json
```

For other GPU work, wait for the current work and hold the GPU for the command:

```sh
vhotseat hold <mode> --wait 900 -- <cmd>
```

On Windows invoke Git Bash by its full path `C:\Program Files\Git\usr\bin\bash.exe`; bare `bash` is WSL.

Run long GPU commands in the foreground and wait for them to finish. If your harness sets a shell command timeout, make it long enough for the whole batch (hours for video); `comfy-render`'s own `--timeout` is a no-progress limit and gives up by itself. Never background one and end your turn to wait; nothing will wake you and the work stalls. Run each GPU command once; report a failure, do not retry. Never stop or restart Strata. Never cancel or delete prompts you did not queue. Never read model folders: ask the ComfyUI API for model names. Do not download models.

## Waiting and bookings

“Strata is answering” means the LLM is handling a request; let it finish. A refusal means the GPU cannot be handed over now. A 503 with `Retry-After` means the service is temporarily unavailable while the GPU is busy; the calling harness should respect that delay. Use `--wait` or `--hold-wait` to wait rather than fail immediately; do not write a polling or retry loop.

For scheduled windows, find availability, book a window, and submit a booked ticket (replace the placeholders). `--as` takes your own name, for example `lead-38`:

```sh
vhotseat when comfy --class booked --for 30m --as <you>
vhotseat book <you> <start> <end> --workload comfy --class booked
vhotseat run comfy --class booked --for 30m --as <you> --wait 900 -- <cmd>
```

Inspect `vhotseat why`, `vhotseat status`, and the dashboard at http://127.0.0.1:8765.

## JSON and exit codes

With `--json`, stdout is exactly one array and progress goes to stderr. Each item has a one-based job and status; the other keys are optional.

Keys: `job`, `status`, `prompt_id`, `files`, `errors`

Statuses: `queued`, `done`, `failed`, `rejected`, `timeout`, `cancelled`, `not submitted`

| Constant | Code | Meaning |
| --- | --- | --- |
| `EXIT_OK` | 0 | All jobs done |
| `EXIT_FAILED` | 1 | A job was rejected or failed |
| `EXIT_USAGE` | 2 | Usage error or ComfyUI unreachable |
| `EXIT_TIMEOUT` | 3 | No progress for --timeout; its pending jobs were removed; report it |
| `EXIT_CANCELLED` | 4 | Cancelled by a person |
| `EXIT_YIELDED` | 5 | Internal to `--hold` |
