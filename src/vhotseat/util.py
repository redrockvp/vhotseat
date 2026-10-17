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
LOG_PREFIX = "gpu-workload"


def log(msg: str) -> None:
    print(f"{LOG_PREFIX}: {msg}", file=sys.stderr)


def sleep(seconds: float) -> None:
    time.sleep(seconds * SCALE)


def http(method: str, url: str, body: dict | None = None, timeout: float = 5.0):
    """Return (status, parsed JSON or text), or (None, error string) if unreachable."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
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
