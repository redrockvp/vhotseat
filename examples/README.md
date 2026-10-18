# Example configs, one per workload

Each file is a complete `config.yaml` with a single workload, so it can be
read on its own. To share a GPU, put two or more of them under one
`workloads:` key in `~/.config/vhotseat/config.yaml`.
[config.example.yaml](../config.example.yaml) is that, for Strata and ComfyUI,
with every key commented.

| File | Workload | Driver | Gate |
| --- | --- | --- | --- |
| [strata.yaml](strata.yaml) | [Strata](https://github.com/Niko1221/Strata), a local LLM server | `http` (config only) | `hook`: Strata's `before_load` |
| [comfyui.yaml](comfyui.yaml) | [ComfyUI](https://github.com/comfyanonymous/ComfyUI) | `comfyui` (built in) | `plugin`: the vhotseat guard node |
| [ollama-proxy.yaml](ollama-proxy.yaml) | [Ollama](https://ollama.com) behind a proxy you build | `http` | `proxy`: [docs/guides/proxy-gate.md](../docs/guides/proxy-gate.md) |

Every file here is validated by `tests/test_examples.py`.
