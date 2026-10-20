"""GPU backends: what the card has free, and how much VRAM each workload holds.

A backend reads the card's totals and its compute processes (pid, MiB). Each
process is then attributed to a workload by its systemd unit (the cgroup it
runs in) or by a substring of its command line; the rest is "other".

On Windows (WDDM) the driver reports no per-process VRAM: NVML and nvidia-smi
say N/A. The same figure Task Manager shows comes from the GPU performance
counters instead (`\\GPU Process Memory(*)\\Dedicated Usage`), filled in for
each of the card's processes. There are no cgroups there; a process is matched
by its executable's path plus its command line.
"""

import ctypes
import os
import re
import subprocess
from pathlib import Path


class GpuError(Exception):
    pass


def _read_proc_windows(pid: int) -> tuple[str, str]:
    """("", executable path + command line) of a process, empty if it's gone or
    not ours to read."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    ntdll = ctypes.WinDLL("ntdll")
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return "", ""
    h = ctypes.c_void_p(h)
    try:
        image = ctypes.create_unicode_buffer(1024)
        n = ctypes.c_ulong(1024)
        exe = image.value if k32.QueryFullProcessImageNameW(h, 0, image, ctypes.byref(n)) else ""
        size = ctypes.c_ulong(0)
        ntdll.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(size))  # ProcessCommandLineInformation
        cmd = ""
        if size.value:
            buf = ctypes.create_string_buffer(size.value)
            if ntdll.NtQueryInformationProcess(h, 60, buf, size, ctypes.byref(size)) == 0:
                length = ctypes.c_ushort.from_buffer(buf).value   # UNICODE_STRING.Length, in bytes
                ptr = ctypes.c_void_p.from_buffer(buf, 8).value     # .Buffer, after 4 bytes of padding
                cmd = ctypes.wstring_at(ptr, length // 2) if ptr else ""
        return "", f"{exe} {cmd}".strip()
    finally:
        k32.CloseHandle(h)


class _ProcEntry(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_ulong), ("th32Threads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260)]


def process_names() -> dict[int, str]:
    """Windows: pid -> exe name for every process, from a Toolhelp snapshot.

    Unlike OpenProcess, a snapshot needs no handle, so it still names processes this
    process cannot open - an elevated workload, for example, whose path and command
    line are unreadable and whose VRAM would otherwise be unattributable."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = ctypes.c_void_p(k32.CreateToolhelp32Snapshot(0x2, 0))   # TH32CS_SNAPPROCESS
    if not snap or ctypes.cast(snap, ctypes.c_void_p).value in (None, -1):
        return {}
    entry = _ProcEntry()
    entry.dwSize = ctypes.sizeof(_ProcEntry)
    out: dict[int, str] = {}
    try:
        rc = k32.Process32FirstW(snap, ctypes.byref(entry))
        while rc:
            out[entry.th32ProcessID] = entry.szExeFile
            rc = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


class _PdhItem(ctypes.Structure):  # PDH_FMT_COUNTERVALUE_ITEM_W with a LONGLONG value
    _fields_ = [("name", ctypes.c_wchar_p), ("status", ctypes.c_ulong), ("value", ctypes.c_longlong)]


PDH_MORE_DATA = 0x800007D2
PDH_FMT_LARGE = 0x400
_INSTANCE = re.compile(r"pid_(\d+)_luid_")


def wddm_process_vram() -> dict[int, int]:
    """Windows: dedicated VRAM per pid (MiB), from the GPU performance counters,
    summed over the process's adapters. Raises GpuError if they can't be read.
    Meant for a single NVIDIA card: with several, a process's VRAM on each is added up."""
    pdh = ctypes.WinDLL("pdh")
    query, counter = ctypes.c_void_p(), ctypes.c_void_p()
    if pdh.PdhOpenQueryW(None, None, ctypes.byref(query)) != 0:
        raise GpuError("PDH: can't open a query")
    try:
        rc = pdh.PdhAddEnglishCounterW(query, r"\GPU Process Memory(*)\Dedicated Usage", None,
                                       ctypes.byref(counter))
        if rc != 0:
            raise GpuError(f"PDH: no GPU Process Memory counters (0x{rc & 0xFFFFFFFF:08X})")
        if (rc := pdh.PdhCollectQueryData(query)) != 0:
            raise GpuError(f"PDH: collecting failed (0x{rc & 0xFFFFFFFF:08X})")
        size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
        rc = pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_LARGE, ctypes.byref(size), ctypes.byref(count), None)
        if rc & 0xFFFFFFFF != PDH_MORE_DATA:
            raise GpuError(f"PDH: reading failed (0x{rc & 0xFFFFFFFF:08X})")
        buf = ctypes.create_string_buffer(size.value)
        rc = pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_LARGE, ctypes.byref(size), ctypes.byref(count), buf)
        if rc != 0:
            raise GpuError(f"PDH: reading failed (0x{rc & 0xFFFFFFFF:08X})")
        out: dict[int, int] = {}
        for item in (_PdhItem * count.value).from_buffer(buf):
            if (m := _INSTANCE.match(item.name or "")) and item.status in (0, 1):  # PDH_CSTATUS_VALID/NEW_DATA
                out[int(m.group(1))] = out.get(int(m.group(1)), 0) + item.value
        return {pid: b // 2**20 for pid, b in out.items()}
    finally:
        pdh.PdhCloseQuery(query)


def read_proc(pid: int) -> tuple[str, str]:
    """(cgroup path, command line) of a process, empty strings if it's gone."""
    if os.name == "nt":
        return _read_proc_windows(pid)
    try:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text().split("::", 1)[-1].strip()
    except OSError:
        cgroup = ""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        cmd = ""
    return cgroup, cmd


def classify(total: int, free: int, procs: list[tuple[int, int | None, str]],
             selectors: dict[str, tuple[str, str] | None], procinfo=read_proc) -> tuple[dict, list]:
    """(the attribution summary, and one row per process holding memory).

    selectors: workload -> ("unit", "comfyui.service") or ("process", "ComfyUI/main.py").
    procs: (pid, MiB or None if the driver can't say, name the GPU reader reports)."""
    out = {"total_mib": total, "free_mib": free, **{f"{n}_mib": 0 for n in selectors}, "other_mib": 0}
    rows = []
    for pid, mib, name in procs:
        if mib is None:
            continue
        cgroup, cmd = procinfo(pid)
        units = cgroup.split("/")
        key, owner = "other_mib", None
        for n, sel in selectors.items():
            if sel and ((sel[0] == "unit" and sel[1] in units) or (sel[0] == "process" and sel[1] in (cmd or name))):
                key, owner = f"{n}_mib", n
                break
        out[key] += mib
        rows.append({"pid": pid, "mib": mib, "name": name, "workload": owner})
    return out, rows


def attribute(total: int, free: int, procs: list[tuple[int, int | None, str]],
              selectors: dict[str, tuple[str, str] | None], procinfo=read_proc) -> dict:
    """Snapshot keyed "<workload>_mib", plus total_mib, free_mib, other_mib."""
    return classify(total, free, procs, selectors, procinfo)[0]


def holders(total: int, free: int, procs: list[tuple[int, int | None, str]],
            selectors: dict[str, tuple[str, str] | None], procinfo=read_proc, limit: int = 0) -> list[dict]:
    """Who holds the card, biggest first: pid, MiB, process name, and the workload it is
    attributed to (None for processes nothing manages - a desktop, a game, an app)."""
    rows = classify(total, free, procs, selectors, procinfo)[1]
    rows.sort(key=lambda r: -r["mib"])
    return rows[:limit] if limit else rows


class NvidiaSmi:
    name = "nvidia-smi"

    def __init__(self, index: int = 0):
        self.index = index

    def _query(self, what: str) -> str:
        return subprocess.run(
            ["nvidia-smi", "-i", str(self.index), f"--query-{what}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout

    def health(self) -> dict:
        try:
            line = self._query("gpu=name,utilization.gpu,temperature.gpu,power.draw,power.limit").splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError) as e:
            return {"error": f"nvidia-smi failed: {e}"}
        name, *values = (x.strip() for x in line.split(","))

        def num(v):
            try:
                return round(float(v), 1)
            except ValueError:
                return None  # "[N/A]"
        return dict(zip(HEALTH_KEYS, [name, *map(num, values)], strict=False))

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


# What health() reports, each None when the card can't say.
HEALTH_KEYS = ("name", "util_pct", "temp_c", "power_w", "power_limit_w")


class _Utilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


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
            self.lib = ctypes.CDLL("nvml.dll" if os.name == "nt" else "libnvidia-ml.so.1")
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

    def health(self) -> dict:
        """The card's model, load (%), temperature (°C), power draw and limit (W)."""
        def call(fn, *args) -> bool:
            f = getattr(self.lib, fn, None)
            return f is not None and f(self.handle, *args) == 0

        out = dict.fromkeys(HEALTH_KEYS)
        buf = ctypes.create_string_buffer(96)
        if call("nvmlDeviceGetName", buf, 96):
            out["name"] = buf.value.decode(errors="replace")
        util = _Utilization()
        if call("nvmlDeviceGetUtilizationRates", ctypes.byref(util)):
            out["util_pct"] = util.gpu
        v = ctypes.c_uint()
        if call("nvmlDeviceGetTemperature", 0, ctypes.byref(v)):  # 0: the GPU die
            out["temp_c"] = v.value
        if call("nvmlDeviceGetPowerUsage", ctypes.byref(v)):     # mW
            out["power_w"] = round(v.value / 1000, 1)
        if call("nvmlDeviceGetEnforcedPowerLimit", ctypes.byref(v)):
            out["power_limit_w"] = round(v.value / 1000, 1)
        return out

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


class WddmVram:
    """Wraps a reader on Windows: the processes it lists with no VRAM figure (all of
    them, under WDDM) get the one from the GPU performance counters, and the ones it
    lists with no name (all of them, under NVML) get the one from a process snapshot."""

    def __init__(self, reader, per_pid=wddm_process_vram, names=process_names):
        self.reader = reader
        self.name = reader.name
        self.per_pid = per_pid
        self.names = names

    def health(self) -> dict:
        return self.reader.health()

    def read(self) -> tuple[int, int, list]:
        total, free, procs = self.reader.read()
        vram = None if all(mib is not None for _, mib, _ in procs) else self.per_pid()
        names = self.names() if any(not name for _, _, name in procs) else {}
        out = []
        for pid, mib, name in procs:
            if mib is None and vram is not None:
                mib = vram.get(pid)
            out.append((pid, mib, name or names.get(pid, "")))
        return total, free, out


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

    def read_all(self, selectors: dict, limit: int = 8) -> tuple[dict, list]:
        """The attribution sums and the named processes behind them, from one read:
        when `other_mib` is big, what to look at is usually a name, not a number."""
        try:
            total, free, procs = self.reader.read()
        except GpuError as e:
            return ({"total_mib": None, "free_mib": None, **{f"{n}_mib": 0 for n in selectors},
                     "other_mib": 0, "error": str(e)}, [])
        summary, rows = classify(total, free, procs, selectors, self.procinfo)
        rows.sort(key=lambda r: -r["mib"])
        return summary, rows[:limit]

    def health(self) -> dict:
        """Model, load, temperature and power, for the dashboard; {} if the backend can't say."""
        try:
            return self.reader.health() if hasattr(self.reader, "health") else {}
        except Exception as e:  # noqa: BLE001 - only ever shown, never decided on
            return {"error": str(e)}


def open_backend(kind: str = "auto", index: int = 0) -> Backend:
    """nvml, nvidia-smi, or auto (NVML, falling back to nvidia-smi)."""
    wrap = WddmVram if os.name == "nt" else (lambda r: r)
    if kind in ("auto", "nvml"):
        try:
            return Backend(wrap(Nvml(index)))
        except GpuError:
            if kind == "nvml":
                raise
    return Backend(wrap(NvidiaSmi(index)))
