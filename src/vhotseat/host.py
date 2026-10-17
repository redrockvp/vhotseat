"""Service backends: start, check and restart the process behind a workload,
and read its memory. Plus the machine's RAM."""

import subprocess
from pathlib import Path

from .errors import SwitchError

NO_MEMORY = {"ram_mib": None, "swap_mib": None, "file_mib": None}


class Systemd:
    """A systemd unit (`systemctl --user` when user=True). Its memory comes from
    the unit's cgroup."""

    def __init__(self, unit: str, user: bool = True):
        self.name = unit.removesuffix(".service")
        self.unit = f"{self.name}.service"
        self._ctl = ["systemctl", "--user"] if user else ["systemctl"]

    def is_active(self) -> bool:
        return subprocess.run([*self._ctl, "is-active", "--quiet", self.name]).returncode == 0

    def _run(self, verb: str) -> None:
        r = subprocess.run([*self._ctl, verb, self.name], capture_output=True, text=True)
        if r.returncode != 0:
            raise SwitchError(f"{' '.join(self._ctl)} {verb} {self.name} failed: {r.stderr.strip()}")

    def start(self) -> None:
        self._run("start")

    def restart(self) -> None:
        self._run("restart")

    def memory(self) -> dict:
        """The unit's anonymous memory and swap (MiB), from its cgroup. Page cache is left
        out of ram_mib: the kernel drops it under pressure. file_mib (page cache charged
        to the unit) is reported on its own, to see whether that assumption holds."""
        out = dict(NO_MEMORY)
        r = subprocess.run([*self._ctl, "show", self.name, "-p", "ControlGroup", "--value"],
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


class NoService:
    """A workload vhotseat doesn't start or restart (it runs some other way)."""

    name = None
    unit = None

    def is_active(self) -> bool:
        return True

    def start(self) -> None:
        pass

    def restart(self) -> None:
        raise SwitchError("it has no service configured, so vhotseat can't restart it")

    def memory(self) -> dict:
        return dict(NO_MEMORY)


def service(kind: str | None, unit: str | None):
    if kind == "systemd-user":
        return Systemd(unit, user=True)
    if kind == "systemd":
        return Systemd(unit, user=False)
    return NoService()


def machine_ram() -> dict:
    """Machine RAM and swap (MiB) from /proc/meminfo. `available_mib` counts page
    cache the kernel can drop, so it's what a new process can actually get."""
    out = {"total_mib": None, "available_mib": None, "swap_total_mib": None, "swap_used_mib": None}
    try:
        info = {k: int(v.split()[0]) // 1024 for k, v in
                (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
        out.update(total_mib=info["MemTotal"], available_mib=info["MemAvailable"],
                   swap_total_mib=info["SwapTotal"], swap_used_mib=info["SwapTotal"] - info["SwapFree"])
    except (OSError, KeyError, ValueError) as e:
        out["error"] = f"/proc/meminfo: {e}"
    return out
