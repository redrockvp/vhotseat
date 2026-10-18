# Example: a VRAM leak in TTS Audio Suite, from symptom to upstream fix

A worked example of the `vhotseat-vram-leak` skill. The machine shared one
16 GB GPU between a local LLM (Strata) and ComfyUI, with the
[TTS Audio Suite](https://github.com/diodiogod/TTS-Audio-Suite) custom node
pack rendering speech with IndexTTS-2.5. Fixed upstream in **5.10.4**
([PR #373](https://github.com/diodiogod/TTS-Audio-Suite/pull/373)); suite
versions up to 5.10.3 have the leak.

## Symptom

After an IndexTTS render, ComfyUI's `POST /free` reported "freed 4804MB", but
ComfyUI still held about 2 GB, above its `idle_vram_mib` of 1500. Every LLM
load after a TTS render was refused:

```
strata-load (allowed=False, reason=could not free ComfyUI's 2066 MiB:
  timed out after 20s waiting for ComfyUI to release VRAM (below 1500 MiB))
```

With `allow_restart: true` vhotseat restarts ComfyUI instead, which works but
adds ~20 s to that handover, and the leak still costs ComfyUI 2 GB whenever its
own model manager swaps the TTS model out for another one.

## Diagnosis

Reproduced standalone: load IndexTTS2 the way the suite does, render once
under `torch.inference_mode()` (as ComfyUI does), call the suite's unload,
then list the `gc.get_objects()` tensors still `is_cuda`.

The suite wraps the IndexTTS2 engine (`engines/index_tts/indextts/infer_v2_5.py`)
for ComfyUI's model manager. On unload, the wrapper calls `model.to("cpu")` if
the object has a `to()`, and otherwise moves each `nn.Module` attribute one by
one. `IndexTTS2` had no `to()`, so only those modules moved. Left on the GPU:

| Left behind | Size |
| --- | --- |
| `qwen_emo` (QwenEmotion, a Qwen 0.6B text-to-emotion model held in a plain attribute, so never moved) | 1137 MiB |
| Plain tensor attributes inside modules, which `Module.to()` skips: old-style `weight_norm` weights, a `causal_mask` (64 MiB), `freqs_cis` | ~180 MiB |
| Cached reference-voice conditioning, emotion and speaker matrices | ~8 MiB |
| Allocator cache held because of the above | the rest |

## Fix

A `to(device)` method on `IndexTTS2`, which the wrapper then uses for both
unload and reload. It moves every model module plus the plain tensor
attributes inside them, QwenEmotion if loaded, and the emotion and speaker
matrices, and drops the cached reference-voice conditioning (rebuilt on the
next render).

| After a render and unload | ComfyUI VRAM |
| --- | --- |
| Before | 2066 MiB: LLM refused |
| **Patched** | **~340 MiB** standalone, ~500 MiB in ComfyUI |
| Floor: any process that has used CUDA | ~270 MiB |

## Recording it until upstream shipped it

While the fix was local, every changed spot carried `LOCAL PATCH (vhotseat)`,
and the config had:

```yaml
patches:
  - name: TTS Audio Suite IndexTTS unload
    file: <ComfyUI>/custom_nodes/TTS-Audio-Suite/engines/index_tts/indextts/infer_v2_5.py
    marker: "LOCAL PATCH (vhotseat)"
    doc: ~/.config/vhotseat/patches/tts-audio-suite.md
    fixed_upstream_in: {version: 5.10.4, version_file: <ComfyUI>/custom_nodes/TTS-Audio-Suite/pyproject.toml}
```

When the suite updated to 5.10.4, `vhotseat doctor` reported that the
installed version had the fix upstream and the patch could go, before anyone
looked; the entry was then removed.

## If it comes back

Other engines in the suite are wrapped the same way, so each needs a `to()`
that moves everything. The symptom is the same: ComfyUI above `idle_vram_mib`
after `/free`, or the event log showing ComfyUI restarted because it kept VRAM
after `/free`, right after renders with that engine. Diagnose it the same way.
