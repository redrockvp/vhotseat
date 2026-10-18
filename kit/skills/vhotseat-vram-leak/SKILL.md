---
name: vhotseat-vram-leak
description: Diagnose and fix a GPU workload whose release "succeeds" but whose VRAM doesn't drop, so vhotseat's handovers time out, get refused, or need a restart every time. Use when `vhotseat` reports "timed out waiting for X to release VRAM", a workload keeps restarting after renders, or the user says VRAM stays high after unloading a model, often after using a particular custom node or extension.
---

# A release that leaves VRAM behind

vhotseat releases a workload (ComfyUI's `/free`, an LLM server's unload) and
then waits until its VRAM drops below a threshold (`confirm` / `idle_vram_mib`).
If something inside the workload keeps tensors on the GPU, the release reports
success but the VRAM never drops: every handover then waits, is refused, or
(with `allow_restart`) restarts the workload each time. The cause is almost
always one extension or node that doesn't move all of its model to the CPU.

A worked example, from diagnosis to an upstream fix:
`docs/examples/vram-leak-tts-audio-suite.md` in the vhotseat repo.

## Rules

- **Ask before changing anything outside vhotseat**, in particular someone
  else's code. Show the diff first.
- **Back up every file you patch** (`cp X X.pre-vhotseat`).
- **Reproduce before you fix**, and measure after: a fix is the VRAM number
  dropping, not the code looking right.
- Work while the GPU is idle, and stop anything you started before you hand
  back.

## 1. Pin down the trigger

From `~/.local/state/vhotseat/events.jsonl` and `vhotseat status`, find what
was held after release (MiB) and what ran just before. Then, from a freshly
restarted workload, run one job of each kind the user runs, release after each
(`vhotseat switch none`), and note the VRAM. The job after which it stays high
is the trigger; narrow it to one node or extension.

Expect a floor: a process that has used CUDA keeps a few hundred MiB for its
context until it exits. That's not a leak; anything well above it is.

## 2. Reproduce standalone

Write a short script outside the server: load the extension's model the way
the server does (under `torch.inference_mode()` for ComfyUI), run it once, call
the same unload the server calls, then list what is still on the GPU:

```python
import gc, torch
gc.collect(); torch.cuda.empty_cache()
left = [o for o in gc.get_objects() if torch.is_tensor(o) and o.is_cuda]
print(sum(t.numel() * t.element_size() for t in left) / 2**20, "MiB in", len(left), "tensors")
for t in sorted(left, key=lambda t: -t.numel() * t.element_size())[:20]:
    print(tuple(t.shape), t.dtype, [type(r).__name__ for r in gc.get_referrers(t)][:3])
```

Follow the biggest tensors' referrers back to the attribute that holds them.
Common causes:

- the wrapped object has no `.to()`, so the unload moves only its `nn.Module`
  attributes, and a sub-model held in a plain attribute stays;
- plain tensor attributes inside a module (buffers not registered as such,
  caches, masks), which `Module.to()` skips;
- cached conditioning or embeddings kept between runs.

**Check:** the script reproduces the number you saw in the server.

## 3. Fix

The smallest fix in the extension: usually a `to(device)` on its engine that
moves every part, or dropping a cache on unload. Mark every changed spot with
a comment containing `LOCAL PATCH (vhotseat)`. Rerun the standalone script:
the leftover should be at the floor.

## 4. Record it

A patch to someone else's code disappears with their next update, and the
symptom comes back far from the cause. Save the diff next to the config, and
add an entry to `~/.config/vhotseat/config.yaml`:

```yaml
patches:
  - name: <extension> unload
    file: <path to the patched file>
    marker: "LOCAL PATCH (vhotseat)"
    doc: ~/.config/vhotseat/patches/<extension>.md     # the diff and why
    # once upstream ships it, so doctor says the patch can go:
    # fixed_upstream_in: {version: X.Y.Z, version_file: <extension>/pyproject.toml}
```

## 5. Send it upstream

Offer to open an issue or pull request on the extension with the standalone
reproduction and the before/after numbers. Once released, the local patch and
its entry can go; `doctor` says so when `fixed_upstream_in` is set.

## 6. Verify

1. In the real server: run the trigger job, release, and check VRAM is at the
   floor (`vhotseat status`).
2. A handover after that job: `vhotseat switch OTHER` succeeds without a
   restart (the event log shows no restart).
3. `vhotseat doctor`: the patch shows as applied, no FAIL.

Report the before and after numbers to the user.
