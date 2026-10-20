"""Service backends: start, check and restart the process behind a workload, read
its memory, and enumerate what's running on the machine."""

import ctypes
import os
import subprocess
from pathlib import Path

from .errors import SwitchError

NO_MEMORY = {"ram_mib": None, "swap_mib": None, "file_mib": None}

_k32 = None


def _kernel32():
    global _k32
    if _k32 is None:
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return _k32


# --- what's running -------------------------------------------------------------
#
# Who is on the machine, not only who is on the card. Claims (a game, somebody
# else's process) and the attribution of a workload whose image path can't be read
# both start here. A Toolhelp snapshot needs no process handle, so it names the
# processes OpenProcess refuses - an elevated server, for example, whose path and
# command line are unreadable and whose VRAM would otherwise be unattributable.

class _ProcEntry(ctypes.Structure):
    """PROCESSENTRY32W, as CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS) fills it."""
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.c_ulong), ("th32Threads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260)]


def _exe_path(pid: int) -> str | None:
    """The image path of `pid`; None where it can't be read (another user's process,
    or one running elevated)."""
    k32 = _kernel32()
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    h = k32.OpenProcess(0x1000, False, pid)                 # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.c_ulong(len(buf))
        if not k32.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf, ctypes.byref(n)):
            return None
        return buf.value[:n.value]
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def _snapshot_windows() -> dict[int, dict]:
    k32 = _kernel32()
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)             # TH32CS_SNAPPROCESS
    if not snap or ctypes.cast(snap, ctypes.c_void_p).value in (None, -1):
        return {}
    entry = _ProcEntry()
    entry.dwSize = ctypes.sizeof(_ProcEntry)
    out: dict[int, dict] = {}
    try:
        rc = k32.Process32FirstW(snap, ctypes.byref(entry))
        while rc:
            out[entry.th32ProcessID] = {"pid": entry.th32ProcessID,
                                        "ppid": entry.th32ParentProcessID,
                                        "name": entry.szExeFile, "exe": None}
            rc = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    for p in out.values():          # paths after the scan: a denied read costs a
        p["exe"] = _exe_path(p["pid"])   # syscall, never a missing process
    return out


def _snapshot_posix() -> dict[int, dict]:
    out: dict[int, dict] = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            name = Path(entry.path, "comm").read_text().strip() or None
            ppid = int(Path(entry.path, "stat").read_text().rsplit(") ", 1)[1].split()[1])
            exe = os.readlink(Path(entry.path, "exe"))
        except (OSError, ValueError, IndexError):
            continue                # vanished mid-scan
        out[int(entry.name)] = {"pid": int(entry.name), "ppid": ppid, "name": name, "exe": exe}
    return out


def processes() -> dict[int, dict]:
    """pid -> {pid, ppid, name, exe}: who is running right now. `exe` is None where it
    can't be read, so a claim or an attribution decision never rests on it alone."""
    return _snapshot_windows() if os.name == "nt" else _snapshot_posix()


class Systemd:
    """A systemd unit (`systemctl --user` when user=True). Its memory comes from
    the unit's cgroup."""

    can_start = True        # vhotseat can start and restart this one itself

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
    can_start = False       # it runs some other way: vhotseat must not try to start it

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


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong), ("avail_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                ("avail_extended_virtual", ctypes.c_ulonglong)]


def _machine_ram_windows() -> dict:
    """Windows: GlobalMemoryStatusEx. `available_mib` counts the standby list (cache
    Windows hands out on demand), like MemAvailable. There's no swap as such: the
    page file is the commit limit above physical RAM, and what's used of it is
    the commit charge that RAM doesn't cover."""
    out = {"total_mib": None, "available_mib": None, "swap_total_mib": None, "swap_used_mib": None}
    st = _MemoryStatusEx()
    st.length = ctypes.sizeof(st)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        out["error"] = f"GlobalMemoryStatusEx failed (error {ctypes.GetLastError()})"
        return out
    mib = 2**20
    committed = st.total_page_file - st.avail_page_file
    in_ram = st.total_phys - st.avail_phys
    out.update(total_mib=st.total_phys // mib, available_mib=st.avail_phys // mib,
               swap_total_mib=max(0, st.total_page_file - st.total_phys) // mib,
               swap_used_mib=max(0, committed - in_ram) // mib)
    return out


def machine_ram() -> dict:
    """Machine RAM and swap (MiB) from /proc/meminfo. `available_mib` counts page
    cache the kernel can drop, so it's what a new process can actually get."""
    if os.name == "nt":
        return _machine_ram_windows()
    out = {"total_mib": None, "available_mib": None, "swap_total_mib": None, "swap_used_mib": None}
    try:
        info = {k: int(v.split()[0]) // 1024 for k, v in
                (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
        out.update(total_mib=info["MemTotal"], available_mib=info["MemAvailable"],
                   swap_total_mib=info["SwapTotal"], swap_used_mib=info["SwapTotal"] - info["SwapFree"])
    except (OSError, KeyError, ValueError) as e:
        out["error"] = f"/proc/meminfo: {e}"
    return out
