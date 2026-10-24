"""`vhotseat autostart`: keep the dashboard and the sampling up from logon.

Nothing else in vhotseat is a service on purpose: it arbitrates per command, so a
resident process that died quietly would be worse than none. The dashboard is the
one part that has to be running to be worth having - history only accumulates
while something samples, and `vhotseat web` samples while it serves - so this
installs exactly one thing: `vhotseat web` at logon (Task Scheduler on Windows, a
systemd user unit elsewhere), or `vhotseat record` for the sampling without the
page. Undo it with `vhotseat autostart remove`.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import util
from .config import state_dir

NAME = "vhotseat"
RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
VBS = ("' vhotseat autostart: starts {command} with no console window.\n"
       "' Remove it with: vhotseat autostart remove\n"
       'CreateObject("Wscript.Shell").Run """{exe}"" {command}", 0, False\n')

UNIT = """[Unit]
Description=vhotseat: dashboard and GPU sampling
[Service]
ExecStart={exe} {command}
Restart=on-failure
[Install]
WantedBy=default.target
"""


def kind() -> str:
    """How this machine keeps a process up from logon."""
    return "schtasks" if os.name == "nt" else "systemd"


def executable() -> str:
    """The vhotseat the task should run: the one on PATH, else the one running now."""
    return shutil.which("vhotseat") or str(Path(sys.argv[0]).resolve())


def unit_dir() -> Path:
    home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return home / "systemd" / "user"


def _run(run, argv: list[str]) -> tuple[int, str]:
    p = run(argv, capture_output=True, text=True)
    out = (p.stdout or "") .strip() or (p.stderr or "").strip()
    return p.returncode, out


def launcher_path() -> Path:
    return state_dir() / f"{NAME}-autostart.vbs"


def _exec_path(path: str) -> str:
    """A path as systemd's `ExecStart` wants it. The line is split on whitespace before anything is
    run - there is no shell behind a unit - so a `vhotseat` under a directory with a space in it
    (a profile, a mounted volume, a home directory with somebody's name in it) leaves the unit
    holding half a path and a second word systemd has never heard of: `install` says it worked and
    nothing is up after the next logon. systemd takes double quotes, and inside them a backslash
    escapes, so both have to be doubled up to survive as themselves."""
    if path and not any(c.isspace() for c in path) and '"' not in path and "\\" not in path:
        return path
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def install(command: str = "web", exe: str | None = None, at: str | None = None,
            run=subprocess.run, units: Path | None = None, vbs: Path | None = None) -> int:
    """Register `vhotseat <command>` to run at logon, replacing any earlier one."""
    exe, at = exe or executable(), at or kind()
    if at == "schtasks":
        rc, out = _run(run, ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", NAME,
                             "/TR", f'"{exe}" {command}'])
        where = "Task Scheduler (at logon)"
        if rc:
            # A logon trigger needs elevation, which a normal shell doesn't have. The
            # per-user Run key needs none and does the same thing for one user - through a
            # one-line script host launcher, because a console app started from the Run key
            # would keep a terminal window on the desktop all session.
            path = vbs or launcher_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(VBS.format(exe=exe, command=command), encoding="utf-8")
            rc, out = _run(run, ["reg", "add", RUN_KEY, "/v", NAME, "/t", "REG_SZ",
                                 "/d", f'wscript.exe //B "{path}"', "/f"])
            where = (f"{RUN_KEY}\\{NAME} -> {path} (Task Scheduler refused a logon trigger "
                     "without elevation)")
    else:
        path = (units or unit_dir()) / f"{NAME}.service"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(UNIT.format(exe=_exec_path(exe), command=command), encoding="utf-8")
        rc, out = _run(run, ["systemctl", "--user", "daemon-reload"])
        if rc == 0:
            rc, out = _run(run, ["systemctl", "--user", "enable", "--now", f"{NAME}.service"])
        where = str(path)
    if rc:
        util.log(f"couldn't install it: {out or 'command failed'}")
        return 1
    print(f"vhotseat {command} now starts at logon: {NAME} in {where}")
    print("undo with: vhotseat autostart remove")
    return 0


def status(exe: str | None = None, at: str | None = None, run=subprocess.run,
           units: Path | None = None) -> int:
    """Is it registered, and is it running? Exit 0 when it is, 1 when it isn't."""
    at = at or kind()
    if at == "schtasks":
        rc, out = _run(run, ["schtasks", "/Query", "/TN", NAME, "/FO", "LIST"])
        reg, body = _run(run, ["reg", "query", RUN_KEY, "/v", NAME])
        if rc == 0:
            print(f"registered: {NAME} in Task Scheduler")
            for line in out.splitlines():
                if line.lower().startswith(("status", "next run time", "task to run")):
                    print(f"  {line.strip()}")
        elif reg == 0:
            print(f"registered: {NAME} in {RUN_KEY}")
            print("  " + " ".join(body.split()[-2:]))
        else:
            print(f"not registered: {NAME} in Task Scheduler or {RUN_KEY}")
            return 1
        return 0
    path = (units or unit_dir()) / f"{NAME}.service"
    if not path.exists():
        print(f"not registered: no {path}")
        return 1
    rc, out = _run(run, ["systemctl", "--user", "is-active", f"{NAME}.service"])
    print(f"registered: {path}\n  {out or 'installed, not running'}")
    return 0 if rc == 0 else 1


def remove(at: str | None = None, run=subprocess.run, units: Path | None = None,
           vbs: Path | None = None) -> int:
    at = at or kind()
    if at == "schtasks":
        _run(run, ["schtasks", "/Delete", "/F", "/TN", NAME])
        _run(run, ["reg", "delete", RUN_KEY, "/v", NAME, "/f"])
        (vbs or launcher_path()).unlink(missing_ok=True)
    else:
        path = (units or unit_dir()) / f"{NAME}.service"
        _run(run, ["systemctl", "--user", "disable", "--now", f"{NAME}.service"])
        path.unlink(missing_ok=True)
        _run(run, ["systemctl", "--user", "daemon-reload"])
    print(f"removed: {NAME} no longer starts at logon")
    return 0
