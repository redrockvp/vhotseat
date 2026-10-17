"""GPU backends: what the card has free, and how much VRAM each workload holds."""

import subprocess


class NvidiaSmi:
    """Card totals and per-process VRAM from nvidia-smi. A process belongs to a
    workload when its path contains that workload's `proc` substring."""

    def snapshot(self, procs: dict[str, str]) -> dict:
        """{"total_mib", "free_mib", "<name>_mib" for each workload, "other_mib"},
        plus "error" if nvidia-smi failed."""
        out = {"total_mib": None, "free_mib": None, **{f"{n}_mib": 0 for n in procs}, "other_mib": 0}
        try:
            card = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total,memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout.splitlines()[0]
            out["total_mib"], out["free_mib"] = (int(x) for x in card.split(","))
            apps = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError, IndexError, ValueError) as e:
            out["error"] = f"nvidia-smi failed: {e}"
            return out
        for line in apps.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 3 or not parts[2].isdigit():
                continue
            name, mib = parts[1], int(parts[2])
            key = next((f"{n}_mib" for n, sub in procs.items() if sub in name), "other_mib")
            out[key] += mib
        return out
