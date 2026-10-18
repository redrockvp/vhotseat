# Handling ComfyUI updates

The guard node, `integrations/comfyui/vhotseat_guard`, makes each ComfyUI job
take the GPU from the other workloads before it runs. It does that by wrapping
a **private** ComfyUI method, `execution.PromptExecutor.execute_async`.
ComfyUI doesn't promise to keep that method stable, so an update can break the
guard. The `vhotseat-comfyui-update` skill walks an agent through this page.

Below, `$COMFY` is ComfyUI's folder and the service is a systemd user unit
named `comfyui`; adjust both to your setup.

## What breaks, and how you'd notice

| Change in ComfyUI | What happens | Visible as |
| --- | --- | --- |
| `execution.PromptExecutor` or `execute_async` renamed or removed | Guard turns itself **off**; ComfyUI keeps working, unguarded | `vhotseat_guard: OFF, ComfyUI internals changed` in the log at startup |
| `execute_async` gets new parameters | Guard is on, but every job fails with a `TypeError` | Every job errors right after you queue it |
| The worker stops calling `execute_async` once per job | Guard is on but never runs | Jobs run while the LLM is loaded; most likely an out-of-memory error |
| Fields read by `_fail_job` change (`status_messages`, `history_result`, `add_message`) | Only the failure path breaks: a refused job crashes instead of failing cleanly | Test 4 below |

The first row is the safe one. The others are why you should check after every
update rather than wait to notice.

## After every ComfyUI update

1. **Note the current commit, update, then restart:**
   ```sh
   cd $COMFY && git rev-parse --short HEAD    # to roll back to, if needed
   git pull && systemctl --user restart comfyui
   ```

2. **Confirm the guard loaded and is on:**
   ```sh
   journalctl --user -u comfyui --since "-5min" | grep vhotseat_guard
   ```
   Expect `vhotseat_guard: on (…/vhotseat, wait 300s)`. If you see `OFF` or
   an import error, stop here and go to *Fixing it* below.

3. **Check that the method's signature hasn't changed.** The guard mirrors it:
   ```sh
   grep -n "async def execute_async" $COMFY/execution.py
   ```
   Expect `async def execute_async(self, prompt, prompt_id, extra_data={}, execute_outputs=[]):`.
   Also check that the worker still calls it once per job: `main.py` →
   `prompt_worker` → `e.execute(...)` → `asyncio.run(self.execute_async(...))`.

4. **Run the smoke tests.** These take a couple of minutes and need a few GB
   of VRAM. `integrations/comfyui/smoke_job.py` queues a 1-step SDXL job that
   ends in `PreviewImage`, so nothing is written to `output/`; pass a
   checkpoint you have with `--ckpt NAME.safetensors`. It prints
   `success in 9.0s`, or `error in 2.5s: GPU busy, job not run: …`. The tests
   use Strata as the LLM; for another LLM, send it the same kind of requests.

   | # | Do | Expect |
   | --- | --- | --- |
   | 1 | `vhotseat switch strata`, then queue a job | Job succeeds; `vhotseat status` shows `comfy`, Strata unloaded |
   | 2 | Start a long Strata answer, queue a job 2–3 s later | Strata's answer completes; job runs after it and succeeds |
   | 3 | Queue a job, send a Strata request, queue another | Both jobs succeed; each Strata request frees ComfyUI first |
   | 4 | `systemctl --user set-environment VHOTSEAT_GUARD_WAIT_S=2`, restart ComfyUI, start a long Strata answer, queue a job | Job ends `status: error` with `GPU busy, job not run: …` in `/history/<id>`; Strata's answer completes. Then `systemctl --user unset-environment VHOTSEAT_GUARD_WAIT_S` and restart ComfyUI |
   | 5 | `comfy-render` a batch of 2–3 small jobs (a `{"base": …, "jobs": [seeds]}` file ending in `SaveImage`) | Progress lines while it runs, one `ok` line per job with its file, exit code 0 |

   For a long Strata answer:
   ```sh
   curl -s localhost:2335/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"messages":[{"role":"user","content":"Count from 1 to 400."}],"max_tokens":1500}' >/dev/null &
   ```

   Test 5 checks comfy-render, which reads ComfyUI's public `/history` and
   websocket formats rather than private methods, so it breaks less often than
   the guard, but it does depend on them.

5. **Record the commit** you verified (step 1's `git rev-parse` after the
   pull), so the next rollback has a known-good target.

## Fixing it

- **Method renamed:** find what `prompt_worker` in `main.py` now calls per job,
  and wrap that instead. The requirements are:
  - it runs on the worker thread (blocking is fine there; the web server's
    thread must never block);
  - it runs once per job, before any model loads;
  - you can still report a failure that shows up in `/history`.
- **Do not move the guard into `PromptServer.add_on_prompt_handler`.** That
  runs at submit time, on the web server's event loop. Blocking there for up
  to 300 s would freeze ComfyUI's UI and API.
- **To run unguarded until it's fixed:** set `VHOTSEAT_GUARD=0` in the
  unit's environment (`systemctl --user edit comfyui`). Then queue ComfyUI
  work only after running `vhotseat switch comfy` by hand.

## Rolling back ComfyUI

When a fix isn't quick, go back to the last commit the guard was verified on:

```sh
cd $COMFY && git log --oneline -5      # find the previous commit
git checkout <commit> && systemctl --user restart comfyui
```

The guard was last verified by the vhotseat project on ComfyUI v0.39.0.
