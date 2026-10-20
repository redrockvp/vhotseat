"""Small helpers shared by the core and the drivers."""

import json
import sys
import time
import urllib.error
import urllib.request

from .errors import SwitchError

# Multiplies every wait and poll interval. Tests shrink it; the real servers
# change state on a scale of seconds.
SCALE = 1.0
LOG_PREFIX = "vhotseat"
# Also gets every line logged (telemetry keeps them for `vhotseat web`), if set.
LOG_SINK = None


def log(msg: str) -> None:
    print(f"{LOG_PREFIX}: {msg}", file=sys.stderr)
    if LOG_SINK is not None:
        LOG_SINK(msg)


def safe_output() -> None:
    """Never crash on printing: a Windows pipe is cp1252, which has no → or ≤.
    Characters the output can't encode come out as ?."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def window(text) -> float:
    """`7d`, `24h`, `90m`, `30s` or bare seconds, as seconds."""
    t = str(text).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    num, mult = (t[:-1], units[t[-1]]) if t[-1:] in units else (t, 1)
    try:
        value = float(num)
    except ValueError:
        raise ValueError(f"not a length of time: {text!r} (try 90m, 24h, 7d, or seconds)") from None
    if value <= 0:
        raise ValueError(f"not a length of time: {text!r} (needs a positive one)")
    return value * mult


def sleep(seconds: float) -> None:
    time.sleep(seconds * SCALE)


def replace(tmp, path, tries: int = 20) -> None:
    """tmp.replace(path), atomically. On Windows that fails while another process
    has `path` open for its brief read, so it's retried for up to a second."""
    for attempt in range(tries):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.05)


def http(method: str, url: str, body: dict | None = None, timeout: float = 5.0,
         headers: dict | None = None):
    """Return (status, parsed JSON or text), or (None, error string) if unreachable."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        return None, str(getattr(e, "reason", e))
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw.decode(errors="replace")


def wait_until(pred, timeout: float, what: str, poll: float = 1.0, error=SwitchError):
    deadline = time.monotonic() + timeout * SCALE
    while True:
        value = pred()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise error(f"timed out after {timeout:g}s waiting for {what}")
        sleep(poll)
