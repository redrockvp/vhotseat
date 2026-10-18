"""GPU backends: what the card has free, and how much VRAM each workload holds.

A backend reads the card's totals and its compute processes (pid, MiB). Each
process is then attributed to a workload by its systemd unit (the cgroup it
runs in) or by a substring of its command line; the rest is "other".
"""

import ctypes
import subprocess
from pathlib import Path


class GpuError(Exception):
    pass


def read_proc(pid: int) -> tuple[str, str]:
    """(cgroup path, command line) of a process, empty strings if it's gone."""
    try:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text().split("::", 1)[-1].strip()
    except OSError:
        cgroup = ""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        cmd = ""
    return cgroup, cmd


def attribute(total: int, free: int, procs: list[tuple[int, int | None, str]],
              selectors: dict[str, tuple[str, str] | None], procinfo=read_proc) -> dict:
    """Snapshot keyed "<workload>_mib", plus total_mib, free_mib, other_mib.

    selectors: workload -> ("unit", "comfyui.service") or ("process", "ComfyUI/main.py").
    procs: (pid, MiB or None if the driver can't say, name nvidia-smi reports)."""
    out = {"total_mib": total, "free_mib": free, **{f"{n}_mib": 0 for n in selectors}, "other_mib": 0}
    for pid, mib, name in procs:
        if mib is None:
            continue
        cgroup, cmd = procinfo(pid)
        units = cgroup.split("/")
        key = "other_mib"
        for n, sel in selectors.items():
            if sel and ((sel[0] == "unit" and sel[1] in units) or (sel[0] == "process" and sel[1] in (cmd or name))):
                key = f"{n}_mib"
                break
        out[key] += mib
    return out


class NvidiaSmi:
    name = "nvidia-smi"

    def __init__(self, index: int = 0):
        self.index = index

    def _query(self, what: str) -> str:
        return subprocess.run(
            ["nvidia-smi", "-i", str(self.index), f"--query-{what}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout

    def read(self) -> tuple[int, int, list]:
        try:
            total, free = (int(x) for x in self._query("gpu=memory.total,memory.free").splitlines()[0].split(","))
            apps = self._query("compute-apps=pid,process_name,used_memory")
        except (OSError, subprocess.SubprocessError, IndexError, ValueError) as e:
            raise GpuError(f"nvidia-smi failed: {e}") from None
        procs = []
        for line in apps.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 3 and parts[0].isdigit():
                procs.append((int(parts[0]), int(parts[2]) if parts[2].isdigit() else None, parts[1]))
        return total, free, procs


class _Memory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _Process(ctypes.Structure):  # nvmlProcessInfo_t (v2/v3 layout)
    _fields_ = [("pid", ctypes.c_uint), ("used", ctypes.c_ulonglong),
                ("gpu_instance", ctypes.c_uint), ("compute_instance", ctypes.c_uint)]


class _ProcessV1(ctypes.Structure):
    _fields_ = [("pid", ctypes.c_uint), ("used", ctypes.c_ulonglong)]


NVML_ERROR_INSUFFICIENT_SIZE = 7
NOT_AVAILABLE = 2**64 - 1  # usedGpuMemory under WDDM


class Nvml:
    """NVIDIA's management library, called directly: no nvidia-smi process per read."""

    name = "nvml"

    def __init__(self, index: int = 0):
        try:
            self.lib = ctypes.CDLL("libnvidia-ml.so.1")
        except OSError as e:
            raise GpuError(f"NVML not available: {e}") from None
        self._check(self.lib.nvmlInit_v2(), "nvmlInit")
        self.handle = ctypes.c_void_p()
        self._check(self.lib.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(self.handle)),
                    f"no GPU {index}")
        for fn, struct in (("nvmlDeviceGetComputeRunningProcesses_v3", _Process),
                           ("nvmlDeviceGetComputeRunningProcesses_v2", _Process),
                           ("nvmlDeviceGetComputeRunningProcesses", _ProcessV1)):
            if hasattr(self.lib, fn):
                self._procs, self._struct = getattr(self.lib, fn), struct
                break

    def _check(self, rc: int, what: str) -> None:
        if rc != 0:
            raise GpuError(f"NVML {what} failed (error {rc})")

    def driver_version(self) -> str:
        buf = ctypes.create_string_buffer(80)
        self.lib.nvmlSystemGetDriverVersion(buf, 80)
        return buf.value.decode()

    def read(self) -> tuple[int, int, list]:
        mem = _Memory()
        self._check(self.lib.nvmlDeviceGetMemoryInfo(self.handle, ctypes.byref(mem)), "memory info")
        size = 16
        while True:
            arr = (self._struct * size)()
            n = ctypes.c_uint(size)
            rc = self._procs(self.handle, ctypes.byref(n), arr)
            if rc != NVML_ERROR_INSUFFICIENT_SIZE:
                break
            size = n.value + 16
        self._check(rc, "process list")
        procs = [(p.pid, None if p.used == NOT_AVAILABLE else p.used // 2**20, "") for p in arr[:n.value]]
        return mem.total // 2**20, mem.free // 2**20, procs


class Backend:
    """A GPU backend plus attribution: what the arbiter calls."""

    def __init__(self, reader, procinfo=read_proc):
        self.reader = reader
        self.name = reader.name
        self.procinfo = procinfo

    def snapshot(self, selectors: dict) -> dict:
        try:
            total, free, procs = self.reader.read()
        except GpuError as e:
            return {"total_mib": None, "free_mib": None, **{f"{n}_mib": 0 for n in selectors},
                    "other_mib": 0, "error": str(e)}
        return attribute(total, free, procs, selectors, self.procinfo)


def open_backend(kind: str = "auto", index: int = 0) -> Backend:
    """nvml, nvidia-smi, or auto (NVML, falling back to nvidia-smi)."""
    if kind in ("auto", "nvml"):
        try:
            return Backend(Nvml(index))
        except GpuError:
            if kind == "nvml":
                raise
    return Backend(NvidiaSmi(index))
