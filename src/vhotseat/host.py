"""Service backends: start, check and restart the process behind a workload,
and read its memory."""

import subprocess
from pathlib import Path

from .errors import SwitchError


class SystemdUser:
    """Workloads run as `systemctl --user` units; their memory comes from each
    unit's cgroup."""

    def is_active(self, unit: str) -> bool:
        return subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0

    def _run(self, verb: str, unit: str) -> None:
        r = subprocess.run(["systemctl", "--user", verb, unit], capture_output=True, text=True)
        if r.returncode != 0:
            raise SwitchError(f"systemctl --user {verb} {unit} failed: {r.stderr.strip()}")

    def start(self, unit: str) -> None:
        self._run("start", unit)

    def restart(self, unit: str) -> None:
        self._run("restart", unit)

    def memory(self, unit: str) -> dict:
        """A unit's anonymous memory and swap (MiB), from its cgroup. Page cache is left
        out of ram_mib: the kernel drops it under pressure. file_mib (page cache charged
        to the unit) is reported on its own, to see whether that assumption holds."""
        out = {"ram_mib": None, "swap_mib": None, "file_mib": None}
        r = subprocess.run(["systemctl", "--user", "show", unit, "-p", "ControlGroup", "--value"],
                           capture_output=True, text=True)
        cgroup = r.stdout.strip()
        if not cgroup:
            return out
        base = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
        try:
            for line in (base / "memory.stat").read_text().splitlines():
                key, value = line.split()
                if key in ("anon", "file"):
                    out["ram_mib" if key == "anon" else "file_mib"] = int(value) // 2**20
            out["swap_mib"] = int((base / "memory.swap.current").read_text()) // 2**20
        except (OSError, ValueError):
            pass
        return out

    def machine_ram(self) -> dict:
        """Machine RAM and swap (MiB) from /proc/meminfo."""
        out = {"total_mib": None, "available_mib": None, "swap_total_mib": None, "swap_used_mib": None}
        try:
            info = {k: int(v.split()[0]) // 1024 for k, v in
                    (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
            out.update(total_mib=info["MemTotal"], available_mib=info["MemAvailable"],
                       swap_total_mib=info["SwapTotal"], swap_used_mib=info["SwapTotal"] - info["SwapFree"])
        except (OSError, KeyError, ValueError) as e:
            out["error"] = f"/proc/meminfo: {e}"
        return out
