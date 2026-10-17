"""vhotseat: take turns on one GPU.

Workloads (Strata, ComfyUI, …) can't hold VRAM at the same time. Each one asks
for the GPU through a gate before it loads, and the arbiter makes room by
releasing the others, never interrupting work that is running. See
docs/plans/2026-10-14-vhotseat.md for the design.

Layout:
  arbiter   take the GPU for one workload, gate a load, derive who owns it
  lock      the one lock per GPU, with a note saying what its holder is doing
  events    the decision log
  gpu       card totals and per-workload VRAM (nvidia-smi)
  host      services (systemd --user) and memory (cgroups, /proc/meminfo)
  drivers/  how to talk to each workload
  cli       the `vhotseat` CLI
"""
