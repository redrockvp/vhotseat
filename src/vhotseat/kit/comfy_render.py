"""comfy-render: run a batch of ComfyUI workflows and report what came out.

Queues every job up front, waits until all of them have finished, and prints
one line per job: the files it produced, or ComfyUI's own error. It never
crashes on a bad workflow; a job that fails is reported and the rest still run.

    comfy-render JOBS.json [--url URL] [--comfy-dir DIR] [--timeout SECONDS] [--json] [--verbose]
                           [--hold [NAME]] [--hold-wait SECONDS] [--class C] [--as REQUESTER]
                           [--yield-every N]

There is no limit on how long a batch may take: --timeout (default 1800) is how
long it may go with no sign of progress (no job finishing, no node or sampler
step starting, no change in ComfyUI's queue) before it gives up.

JOBS.json is either
  - one workflow:      {"1": {"class_type": ..., "inputs": {...}}, ...}
  - a list of them:    [ {workflow}, {workflow}, ... ]
  - a base + variants: {"base": {workflow},
                        "jobs": [{"5.inputs.seed": 1}, {"5.inputs.seed": 2}]}
    where each variant overrides "NODE.inputs.NAME" paths in a copy of base.

Workflows are ComfyUI's API format (what "Export (API)" saves, and what
POST /prompt takes). If the GPU is shared with an LLM through vhotseat, its
guard node hands the GPU over before each job; nothing here needs to know.
With --hold, the whole batch runs under `vhotseat hold NAME --class C` (NAME defaults to
comfy, C to batch): a ticket, so the queue's serving order decides when the batch gets the
card, and --as records who is asking. Inside that ticket the batch runs in steps of --yield-every
jobs (default 4) and asks `vhotseat should-yield` between them: once a run has held the card
past its class's max hold, the rest of the batch is written back to the outer comfy-render, which
queues a fresh ticket for it (behind whoever was waiting) and carries on. Exit code 5 is only
that hand-back, lived between the two comfy-render processes; the caller never sees it. That
needs the `vhotseat` command (else $VHOTSEAT_BIN); without --hold, comfy-render works with
ComfyUI alone.

ComfyUI is at --url, else $COMFY_URL, else http://127.0.0.1:8188. Saved files
are printed as absolute paths under --comfy-dir (else $COMFY_DIR, else ComfyUI's
own folder, found through its custom_nodes folder); if neither is known, as
paths relative to ComfyUI's folder (output/...).

Exit codes: 0 every job produced output · 1 at least one job was rejected or
failed · 2 bad input file or ComfyUI unreachable · 3 timed out · 4 the user
cancelled at least one job (it outranks 1 and 3: ask them why before re-running).
On exit, pending jobs from this run are removed and a running job gets a short grace period.
With --hold's steps, the jobs of steps not
yet submitted are reported as "not submitted" (they never reached ComfyUI). 5 only comes from --hold, and only between
its two comfy-render processes: the run inside handed the rest of the batch back, and the outer
one re-queued it before answering.
"""

import argparse
import base64
import contextlib
import copy
import json
import os
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from vhotseat.child_process import STOP_GRACE_S, run_with_signals

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
COMFY_DIR: Path | None = Path(os.environ["COMFY_DIR"]) if os.environ.get("COMFY_DIR") else None
POLL_S = 2.0
CLEANUP_GRACE_S = 5.0
# Live progress: at most one line per job this often, unless the running node changes.
PROGRESS_EVERY_S = 10.0
# Give up after this long with no progress at all. Not a limit on the whole batch: a
# batch of long videos runs for hours, and giving up while jobs are still queued makes
# the caller's next request to an LLM sharing the GPU fail, since ComfyUI keeps the GPU
# until they're done.
STALL_TIMEOUT_S = 1800

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_TIMEOUT, EXIT_CANCELLED = 0, 1, 2, 3, 4

# Set by `vhotseat hold` in its command's environment.
HOLD_ENV = "VHOTSEAT_HOLD"
# Set by `vhotseat hold NAME --class C` (Arbiter.run) once its ticket has started: this run is
# inside a ticket, so it may hand the rest of its batch back.
TICKET_ENV = "VHOTSEAT_TICKET"
# Set by comfy-render's outer half on the inner one: where to leave the jobs not done yet, and
# how the part that ran went in the same path plus `.code`. The outer half re-queues that file.
REST_ENV = "COMFY_RENDER_REST"
# The inner half's code for "here is the rest, queue a ticket for it". Only comfy-render's outer
# half reads it; it never reaches a caller.
EXIT_YIELDED = 5
# Set by the outer half on each round after the first: the jobs file this round runs (the rest
# the previous round left), whatever form the caller gave the original in (`--hold=FILE`).
JOBS_ENV = "COMFY_RENDER_JOBS"
# Set by the outer half: this round's first job number and the whole batch's size, so every round
# numbers its jobs as jobs of the batch.
FIRST_ENV, TOTAL_ENV = "COMFY_RENDER_FIRST", "COMFY_RENDER_TOTAL"
# Set by the outer half with --json: where a round leaves its results, so the caller gets ONE
# document for the whole batch, printed by the outer half at the end.
RESULTS_ENV = "COMFY_RENDER_RESULTS"

CANCELLED_BY = "cancelled by the user (ComfyUI or another client)"


# Programs that hold back or cut off the live progress lines when stdout is piped to them.
BUFFERING_READERS = {"tail", "head", "grep", "egrep", "fgrep", "rg", "sort", "uniq", "less", "more"}
SHELLS = {"sh", "bash", "zsh", "dash", "fish"}


class UsageError(Exception):
    pass


def stdout_reader() -> str | None:
    """The name of the program reading our stdout, if it's a pipe and that's knowable.
    Best effort, Linux only. Right after the shell forks a pipeline, the reader may
    not have exec'd yet and still be named after the shell, so look again briefly."""
    for _ in range(10):
        reader = _pipe_reader()
        if reader not in SHELLS:
            return reader
        time.sleep(0.05)
    return reader


def _pipe_reader() -> str | None:
    """The other process with our stdout pipe open for reading, found in /proc
    (which only shows the caller's own processes, as a pipeline's are)."""
    try:
        st = os.fstat(sys.stdout.fileno())
    except (OSError, ValueError):
        return None
    if not stat.S_ISFIFO(st.st_mode) or not os.path.isdir("/proc"):
        return None
    pipe = f"pipe:[{st.st_ino}]"
    for pid in filter(str.isdigit, os.listdir("/proc")):
        if int(pid) == os.getpid():
            continue
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                if os.readlink(f"/proc/{pid}/fd/{fd}") != pipe:
                    continue
                with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                    flags = next(int(line.split()[1], 8) for line in f if line.startswith("flags:"))
                if flags & os.O_ACCMODE == os.O_RDONLY:
                    return Path(f"/proc/{pid}/comm").read_text().strip()
        except (OSError, StopIteration, ValueError):
            continue
    return None


# --- input --------------------------------------------------------------------


def _is_workflow(obj) -> bool:
    return isinstance(obj, dict) and bool(obj) and all(
        isinstance(v, dict) and "class_type" in v for v in obj.values()
    )


def _apply(workflow: dict, overrides: dict) -> dict:
    out = copy.deepcopy(workflow)
    for path, value in overrides.items():
        parts = path.split(".")
        if len(parts) != 3 or parts[1] != "inputs":
            raise UsageError(f"override {path!r}: expected NODE.inputs.NAME")
        node, _, name = parts
        if node not in out:
            raise UsageError(f"override {path!r}: no node {node!r} in base")
        out[node].setdefault("inputs", {})[name] = value
    return out


def load_jobs(path: str) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UsageError(f"{path}: no such file") from None
    except json.JSONDecodeError as e:
        raise UsageError(f"{path}: not valid JSON ({e})") from None

    if _is_workflow(data):
        return [data]
    if isinstance(data, list) and data and all(_is_workflow(w) for w in data):
        return data
    if isinstance(data, dict) and "base" in data:
        base, variants = data["base"], data.get("jobs") or [{}]
        if not _is_workflow(base):
            raise UsageError(f"{path}: 'base' is not an API-format workflow")
        if not isinstance(variants, list) or not all(isinstance(v, dict) for v in variants):
            raise UsageError(f"{path}: 'jobs' must be a list of override objects")
        return [_apply(base, v) for v in variants]
    raise UsageError(
        f"{path}: expected an API-format workflow, a list of them, or "
        '{"base": workflow, "jobs": [overrides]}. A UI-format export (with '
        '"nodes" and "links") won\'t work; use "Export (API)".'
    )


# --- ComfyUI ------------------------------------------------------------------


def _request(method: str, path: str, body=None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        COMFY_URL + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def _validation_errors(body) -> list[str]:
    """Flatten a /prompt 400 into one readable line per problem."""
    if not isinstance(body, dict):
        return [str(body)[:300]]
    lines = []
    for node_id, info in (body.get("node_errors") or {}).items():
        cls = info.get("class_type", "?")
        for err in info.get("errors") or []:
            detail = err.get("details") or ""
            lines.append(f"node {node_id} {cls}: {err.get('message', 'error')}" + (f": {detail}" if detail else ""))
    if not lines:
        err = body.get("error") or {}
        msg = err.get("message") or str(body)[:300]
        detail = err.get("details") or ""
        lines.append(msg + (f" ({detail})" if detail else ""))
    return lines


def comfy_dir() -> Path | None:
    """ComfyUI's folder: --comfy-dir / $COMFY_DIR, else found in what ComfyUI
    reports at /internal/folder_paths, else None."""
    global COMFY_DIR
    if COMFY_DIR is None:
        try:
            code, body = _request("GET", "/internal/folder_paths", timeout=5)
        except (urllib.error.URLError, OSError):
            code, body = 0, {}
        COMFY_DIR = _comfy_dir_from(code, body)
    return COMFY_DIR


def _folders(body: dict, key: str) -> list[Path]:
    return [Path(p) for p in (body.get(key) or []) if isinstance(p, str)]


def _comfy_dir_from(code: int, body: dict) -> Path | None:
    """The install folder out of ComfyUI's list of model folders.

    extra_model_paths.yaml puts model folders on other drives and they are
    listed first, so checkpoints[0] can be somebody else's collection: the
    saved files would then be printed under a folder that never holds them.
    custom_nodes is not a model folder, so <install>/custom_nodes is the
    install folder; among several, the one with its own models/checkpoints in
    the list is the surest."""
    if code != 200 or not isinstance(body, dict):
        return None
    checkpoints = [p for p in _folders(body, "checkpoints") if p.parts[-2:] == ("models", "checkpoints")]
    installs = [p.parent for p in _folders(body, "custom_nodes") if p.parts[-1:] == ("custom_nodes",)]
    model_roots = {p.parent.parent for p in checkpoints}
    for root in installs:
        if root in model_roots:
            return root
    if installs:
        return installs[0]
    return checkpoints[0].parent.parent if checkpoints else None


def _files(outputs: dict) -> list[str]:
    """Every saved file, whatever the output key (images, audio, gifs, …)."""
    files = []
    base = COMFY_DIR or Path()
    for node_out in (outputs or {}).values():
        for value in node_out.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and "filename" in item:
                    root = "temp" if item.get("type") == "temp" else "output"
                    files.append(str(base / root / item.get("subfolder", "") / item["filename"]))
    return files


def _runtime_error(status: dict, verbose: bool) -> str:
    for kind, data in status.get("messages") or []:
        if kind == "execution_error":
            line = (f"node {data.get('node_id')} {data.get('node_type')}: "
                    f"{data.get('exception_message', '').strip()}")
            if verbose and data.get("traceback"):
                # Library internals end the traceback; the frames that say where
                # in the workflow's nodes it broke are the ones under ComfyUI/.
                frames = [f for f in data["traceback"]
                          if (COMFY_DIR is None or str(COMFY_DIR) in f) and "/.venv/" not in f]
                tail = "".join((frames or data["traceback"])[-3:]).rstrip()
                line += "\n    " + "\n    ".join(tail.splitlines())
            return line
    return status.get("status_str") or "failed"


def _interrupted_at(status: dict) -> str | None:
    """The node a running job was stopped at, if it was cancelled rather than failed."""
    for kind, data in status.get("messages") or []:
        if kind == "execution_interrupted":
            return f"node {data.get('node_id')} {data.get('node_type')}"
    return None


def _queue_ids(timeout: float = 30) -> tuple[set[str], set[str]] | None:
    """Running and pending IDs from the same queue snapshot."""
    try:
        code, body = _request("GET", "/queue", timeout=timeout)
    except (OSError, ValueError):
        return None
    if code != 200 or not isinstance(body, dict):
        return None
    def ids(key):
        return {item[1] for item in body.get(key) or [] if isinstance(item, list) and len(item) > 1}

    return ids("queue_running"), ids("queue_pending")


def _cleanup_queue(owned: set[str]) -> None:
    deadline = time.monotonic() + CLEANUP_GRACE_S
    while (remaining := deadline - time.monotonic()) > 0:
        queue = _queue_ids(remaining)
        if queue is not None:
            running, pending = queue
            delete = owned & pending
            if not delete and owned.isdisjoint(running):
                return
            if delete and (remaining := deadline - time.monotonic()) > 0:
                with contextlib.suppress(OSError, ValueError):
                    _request("POST", "/queue", {"delete": sorted(delete)}, timeout=remaining)
        time.sleep(max(0, min(0.2, deadline - time.monotonic())))
    print("comfy-render: cleanup grace expired while waiting for owned prompts", file=sys.stderr)


# --- live progress -------------------------------------------------------------
#
# ComfyUI's HTTP API only says queued/running/done; step counts go out over its
# websocket, to the client_id a job was queued with. Python has no websocket
# client in the standard library, but this is a local, unencrypted socket that
# only *receives* small JSON frames, so a minimal reader is enough. Anything
# going wrong here just means no progress lines: /history stays the source of
# truth for when a job is finished. The websocket's record of each job's outcome
# and saved files is the fallback when /history has lost it (ComfyUI restarted).


class ProgressWatcher(threading.Thread):
    """Reads ComfyUI's websocket and keeps the latest node / step per prompt."""

    def __init__(self, client_id: str, class_types: dict[str, dict[str, str]]):
        super().__init__(name="comfy-progress", daemon=True)
        self.client_id = client_id
        self.class_types = class_types  # prompt_id -> {node_id: class_type}
        self.state: dict[str, dict] = {}  # prompt_id -> {"node", "value", "max"}
        self.results: dict[str, dict] = {}  # prompt_id -> {"outputs", "status": /history-shaped}
        self.lock = threading.Lock()
        self.connected = threading.Event()
        self.sock: socket.socket | None = None
        self.last_activity = time.monotonic()  # last node or step event for one of our jobs

    def connect(self, timeout: float = 5.0) -> bool:
        """Open the websocket. Call before submitting jobs so no event is missed."""
        try:
            url = urllib.parse.urlsplit(COMFY_URL)
            sock = socket.create_connection((url.hostname, url.port or 80), timeout=timeout)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((
                f"GET /ws?clientId={self.client_id} HTTP/1.1\r\nHost: {url.netloc}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(1024)
                if not chunk:
                    return False
                head += chunk
            if b" 101 " not in head.split(b"\r\n", 1)[0]:
                return False
            sock.settimeout(None)
            self.sock = sock
            self._buf = head.split(b"\r\n\r\n", 1)[1]
            self.connected.set()
            return True
        except OSError:
            return False

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frames(self):
        """Yield (opcode, payload) for each complete message; server frames aren't masked."""
        parts, first_op = [], None
        while True:
            b0, b1 = self._read(2)
            fin, op = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if op == 0x8:  # close
                return
            if op in (0x9, 0xA):  # ping / pong: nothing to answer for a reader this short-lived
                continue
            if op != 0x0:
                first_op, parts = op, []
            parts.append(payload)
            if fin:
                yield first_op, b"".join(parts)

    def run(self):
        try:
            for op, payload in self._frames():
                if op != 0x1:  # binary frames are live preview images
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    continue
                self._on_event(msg.get("type"), msg.get("data") or {})
        except (OSError, ConnectionError, struct.error, ValueError):
            pass

    def _on_event(self, kind: str, data: dict) -> None:
        pid = data.get("prompt_id")
        if not pid:
            return
        with self.lock:
            res = self.results.setdefault(pid, {"outputs": {}, "messages": []})
            if kind == "executed" and isinstance(data.get("output"), dict):
                res["outputs"][str(data.get("node"))] = data["output"]
            elif kind in ("execution_success", "execution_error", "execution_interrupted"):
                res["messages"].append([kind, data])
                res["status_str"] = "success" if kind == "execution_success" else "error"
            st = self.state.setdefault(pid, {})
            if kind == "executing" and data.get("node") is not None:
                st.update(node=str(data["node"]), value=None, max=None)
                self.last_activity = time.monotonic()
            elif kind == "progress":
                st.update(node=str(data.get("node") or st.get("node")),
                          value=data.get("value"), max=data.get("max"))
                self.last_activity = time.monotonic()

    def result(self, pid: str) -> dict | None:
        """The job's outcome as /history would show it, if the websocket saw it end."""
        with self.lock:
            res = self.results.get(pid)
            if not res or "status_str" not in res:
                return None
            return {"outputs": dict(res["outputs"]),
                    "status": {"status_str": res["status_str"], "completed": res["status_str"] == "success",
                               "messages": list(res["messages"])}}

    def describe(self, pid: str) -> tuple[str | None, str | None]:
        """(node_id, a one-line description) of what the job is doing right now."""
        with self.lock:
            st = dict(self.state.get(pid) or {})
        node = st.get("node")
        if not node:
            return None, None
        cls = self.class_types.get(pid, {}).get(node, "")
        text = f"node {node} {cls}".rstrip()
        value, total = st.get("value"), st.get("max")
        if value is not None and total:
            text += f" step {value}/{total} ({100 * value // total}%)"
        return node, text

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


# --- run ----------------------------------------------------------------------


def run(jobs: list[dict], timeout: float, verbose: bool, first: int = 1,
        total: int | None = None) -> tuple[list[dict], bool]:
    """Submit every job, then wait until all of them finish or nothing has progressed
    for `timeout` seconds. Returns (results, timed_out).

    `first` and `total` number these jobs inside a bigger batch: a stepped batch still says
    `job 3/7` for the seventh job of the whole batch, not for its third in this step.
    """
    total = len(jobs) if total is None else total
    client_id = f"comfy-render-{uuid.uuid4().hex[:8]}"
    watcher = ProgressWatcher(client_id, {})
    if watcher.connect():
        watcher.start()
    results = []
    try:
        for i, wf in enumerate(jobs, 1):
            _check_stop()
            job = first + i - 1
            try:
                code, body = _request("POST", "/prompt", {"prompt": wf, "client_id": client_id})
            except (urllib.error.URLError, OSError) as e:
                raise UsageError(f"ComfyUI not reachable at {COMFY_URL}: {getattr(e, 'reason', e)}") from None
            if code == 200 and isinstance(body, dict) and body.get("prompt_id"):
                results.append({"job": job, "prompt_id": body["prompt_id"], "status": "queued"})
                watcher.class_types[body["prompt_id"]] = {
                    nid: node.get("class_type", "") for nid, node in wf.items()
                }
            else:
                results.append({"job": job, "status": "rejected", "errors": _validation_errors(body)})
        queued = sum(r["status"] == "queued" for r in results)
        print(f"queued {queued}/{total} job(s)", flush=True)
        return _wait(results, queued, total, watcher, timeout, verbose)
    finally:
        watcher.close()
        owned = {r["prompt_id"] for r in results if r["status"] in ("queued", "timeout")}
        if owned:
            _cleanup_queue(owned)


def _outcome(entry: dict, status: dict, verbose: bool) -> dict:
    """What a job that reached /history ended as."""
    files = _files(entry.get("outputs"))
    if status.get("status_str") == "error":
        node = _interrupted_at(status)
        if node:
            return {"status": "cancelled", "errors": [f"{CANCELLED_BY} while running {node}"],
                    "files": files}
        return {"status": "failed", "errors": [_runtime_error(status, verbose)]}
    if not files:
        return {"status": "failed", "errors": ["finished but saved no files (no Save* output node?)"]}
    return {"status": "done", "files": files}


def _wait(results, queued, total, watcher, timeout, verbose) -> tuple[list[dict], bool]:
    pending = {r["prompt_id"]: r for r in results if r["status"] == "queued"}
    # Progress is a job finishing, a websocket event for one of ours, or ComfyUI's queue
    # changing (our jobs may be waiting behind someone else's, which send us no events).
    last_progress = time.monotonic()
    last_queue: set[str] | None = None
    last_note = time.monotonic()  # "queued N job(s)" was just printed
    shown: dict[str, tuple[str | None, str | None, float]] = {}  # pid -> (node, text, when)
    while pending:
        if time.monotonic() - max(last_progress, watcher.last_activity) > timeout:
            for r in pending.values():
                r.update(status="timeout", errors=[
                    f"no progress for {timeout:g}s; unfinished at timeout"])
            return results, True
        # A job leaves the queue and enters /history in one step, so reading
        # /queue before /history means a job in neither was deleted while queued.
        queue = _queue_ids()
        in_queue = None if queue is None else queue[0] | queue[1]
        if in_queue is not None and in_queue != last_queue:
            last_queue = in_queue
            last_progress = time.monotonic()
        for pid, r in list(pending.items()):
            _check_stop()
            try:
                _, hist = _request("GET", f"/history/{pid}")
            except (urllib.error.URLError, OSError):
                continue  # transient; try again next round
            entry = hist.get(pid) if isinstance(hist, dict) else None
            if not entry:
                if in_queue is None or pid in in_queue:
                    continue
                # Gone from both: ComfyUI was restarted after it finished (a restart
                # wipes /history), or someone deleted it while it was queued.
                entry = watcher.result(pid)
            if not entry:
                r.update(status="cancelled", errors=[f"{CANCELLED_BY} before it started"])
            else:
                status = entry.get("status") or {}
                if not status.get("completed") and status.get("status_str") != "error":
                    continue
                r.update(**_outcome(entry, status, verbose))
            del pending[pid]
            last_progress = time.monotonic()
            finished = queued - len(pending)
            line = f"job {r['job']}/{total} {r['status']}"
            if r["status"] == "cancelled":
                line += f": {r['errors'][0]}"
            print(f"{line} ({finished} of {queued} queued finished)", flush=True)

        now = time.monotonic()
        for pid, r in pending.items():
            node, text = watcher.describe(pid)
            if not text:
                # No node has started. Either jobs ahead of it are running, or it's
                # ComfyUI's turn but the guard is still taking the GPU from another
                # workload (~1 s) and the model is loading (tens of seconds): say so once.
                if watcher.connected.is_set() and pid not in shown:
                    print(f"job {r['job']}/{total} waiting to start (earlier jobs, or the GPU "
                          "handover and model load)", flush=True)
                    shown[pid] = (None, None, now)
                    last_note = now
                continue
            last_node, last_text, when = shown.get(pid, (None, None, 0.0))
            if text != last_text and (node != last_node or now - when >= PROGRESS_EVERY_S):
                print(f"job {r['job']}/{total} running: {text}", flush=True)
                shown[pid] = (node, text, now)
                last_note = now
        if pending and now - last_note > 60:
            # Keep the caller's idle watchdog fed during long renders.
            print(f"waiting on {len(pending)} job(s)…", flush=True)
            last_note = now
        if pending:
            time.sleep(POLL_S)
    return results, False


def summarize(results: list[dict], total: int | None = None) -> str:
    """One line per job. `total` is the whole batch when these results are one step of it, so
    a step's lines still say which job of the batch they are; the tally counts these jobs."""
    lines = []
    n = len(results)
    tot = total if total is not None else n
    for r in results:
        head = f"job {r['job']}/{tot}"
        if r["status"] == "done":
            lines.append(f"{head} ok: " + ", ".join(r["files"]))
        else:
            for err in r.get("errors") or [r["status"]]:
                lines.append(f"{head} {r['status']}: {err}")
            if r.get("files"):  # a job cancelled partway may have saved some already
                lines.append(f"{head} saved before it was cancelled: " + ", ".join(r["files"]))
    ok = sum(r["status"] == "done" for r in results)
    files = sum(len(r.get("files", [])) for r in results)
    lines.append(f"{ok}/{n} job(s) succeeded, {files} file(s)")
    cancelled = sum(r["status"] == "cancelled" for r in results)
    if cancelled:
        lines.append(f"{cancelled} job(s) cancelled by the user: ask them why before re-running")
    return "\n".join(lines)


def steps(jobs: list, n: int) -> list[list]:
    """Consecutive groups of n. n <= 0 is one group: a batch that never yields inside itself."""
    if not jobs:
        return []
    if n <= 0:
        return [list(jobs)]
    return [list(jobs[i:i + n]) for i in range(0, len(jobs), n)]


def should_yield() -> bool:
    """Ask vhotseat whether this run has held the card past its class's max hold. Only exit 0
    means yes: no binary, an error, or any other code means carry on, never drop work."""
    vhotseat = os.environ.get("VHOTSEAT_BIN") or shutil.which("vhotseat")
    if not vhotseat:
        return False
    try:
        return subprocess.call([vhotseat, "should-yield"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
    except Exception:  # noqa: BLE001 - can't ask: carry on
        return False


def _exit_code(results: list[dict], timed_out: bool = False) -> int:
    """One code for a batch: a cancelled job outranks a timeout and a failure."""
    if any(r["status"] == "cancelled" for r in results):
        return EXIT_CANCELLED
    if timed_out:
        return EXIT_TIMEOUT
    return EXIT_OK if all(r["status"] == "done" for r in results) else EXIT_FAILED


def _report(as_json: bool, results: list[dict], total: int | None = None) -> None:
    print(json.dumps(results, indent=1) if as_json else summarize(results, total))


def _write_rest(rest: Path, left: list[dict], done: list[dict]) -> None:
    """The jobs still to do, as plain workflows in order, plus how the part that ran went."""
    rest.parent.mkdir(parents=True, exist_ok=True)
    rest.write_text(json.dumps(left))
    Path(f"{rest}.code").write_text(str(_exit_code(done)))


def _rest_code(rest: Path) -> int:
    """How a yielded round's finished part went; 0 when the file isn't there to say."""
    try:
        return int(Path(f"{rest}.code").read_text())
    except (OSError, ValueError):
        return EXIT_OK


def run_in_steps(args, jobs: list[dict]) -> int:
    """Inside a ticket, in steps of --yield-every: after every step but the last ask vhotseat
    whether this run has held the card too long, and on a yes leave the rest for the outer
    comfy-render (exit 5) instead of running on behind the next workload. A step that timed
    out stops the batch before more jobs are submitted.
    """
    rest = Path(os.environ[REST_ENV])
    first = _env_int(FIRST_ENV, 1)
    total = _env_int(TOTAL_ENV, first - 1 + len(jobs))
    groups = steps(jobs, args.yield_every)
    done: list[dict] = []
    timed_out = False
    for group in groups:
        results, timed_out = run(group, args.timeout, args.verbose, first=first + len(done), total=total)
        done += results
        left = jobs[len(done):]
        yielding = not timed_out and left and should_yield()
        if not args.json:
            print(summarize(results, total), flush=True)        # each step as it ends
        if yielding:
            _write_rest(rest, left, done)
            _json_out(args, done)
            print(f"yielding the card: {len(left)} job(s) left")
            return EXIT_YIELDED
        if timed_out:
            # The steps after this one never reached ComfyUI: say so, job by job.
            skipped = [{"job": first + len(done) + i, "status": "not submitted",
                        "errors": ["not submitted: the batch timed out before its step"]}
                       for i in range(len(left))]
            if skipped and not args.json:
                print(summarize(skipped, total), flush=True)
            done += skipped
            break
    _json_out(args, done)
    return _exit_code(done, timed_out)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _json_out(args, results: list[dict]) -> None:
    """With --json: one document. A round under the outer half leaves its results for it (the
    outer prints the whole batch at the end); on its own it prints them."""
    if not args.json:
        return
    if os.environ.get(RESULTS_ENV):
        Path(os.environ[RESULTS_ENV]).write_text(json.dumps(results))
    else:
        _report(True, results)


def _parser() -> argparse.ArgumentParser:
    """The command line, built apart so `rerun_under_hold` can read --class and --as off argv."""
    p = argparse.ArgumentParser(prog="comfy-render", description=__doc__.split("\n\n")[0])
    p.add_argument("jobs", nargs="?", help="JSON file: a workflow, a list of them, or {base, jobs}")
    p.add_argument("--url", help="ComfyUI's address (default: $COMFY_URL, else http://127.0.0.1:8188)")
    p.add_argument("--comfy-dir", type=Path, metavar="DIR",
                   help="ComfyUI's folder, for absolute output paths (default: $COMFY_DIR, else asked of ComfyUI)")
    p.add_argument("--timeout", type=float, default=STALL_TIMEOUT_S, metavar="SECONDS",
                   help=f"give up after this long with no progress (default {STALL_TIMEOUT_S}); "
                        "a batch that keeps progressing may take any time")
    p.add_argument("--json", action="store_true", help="print the results as JSON")
    p.add_argument("--verbose", action="store_true", help="include traceback tails for runtime errors")
    p.add_argument("--hold", nargs="?", const="comfy", metavar="NAME",
                   help="run the batch under `vhotseat hold NAME` (default comfy): keep the GPU until it's done")
    p.add_argument("--hold-wait", type=float, default=600, metavar="SECONDS",
                   help="with --hold: how long to wait for the GPU's current work to finish (default 600)")
    p.add_argument("--class", dest="cls", default="batch",
                   help="with --hold: the class of the ticket this batch takes (default batch)")
    p.add_argument("--as", dest="requester",
                   help="with --hold: who is asking, recorded on the ticket (default: vhotseat's)")
    p.add_argument("--yield-every", type=int, default=4, metavar="N",
                   help="inside a ticket: jobs per step before asking whether to yield the card (default 4)")
    return p


# Defer signals until the current submission's prompt_id is recorded. Raising in the
# handler could lose the acknowledgement or interrupt cleanup on a second signal.
_stop_signal = 0


def _check_stop() -> None:
    if _stop_signal:
        raise SystemExit(128 + _stop_signal)


@contextlib.contextmanager
def _render_signals():
    global _stop_signal

    def stop(signum, _frame):
        global _stop_signal
        _stop_signal = signum

    with contextlib.ExitStack() as restore:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, *([signal.SIGBREAK] if os.name == "nt" else [])):
                restore.callback(signal.signal, sig, signal.signal(sig, stop))
        try:
            yield
            _check_stop()
        finally:
            _stop_signal = 0


def main(argv: list[str] | None = None) -> int:
    # Preserve paths and node messages even on a Windows pipe with a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = _parser()
    args = p.parse_args(argv)
    if args.jobs is None:
        # `--hold jobs/x.json` (as the skill writes it): --hold's optional NAME took the file.
        if args.hold and (args.hold.lower().endswith(".json") or Path(args.hold).is_file()):
            args.jobs, args.hold = args.hold, "comfy"
        else:
            p.error("the following arguments are required: jobs")
    reader = stdout_reader()
    if reader in BUFFERING_READERS:
        print(f"comfy-render: don't pipe its output (to {reader}): its progress lines are how the "
              "user follows a long render live. Run it as `comfy-render JOBS.json`, with no pipe.",
              file=sys.stderr)
        return EXIT_USAGE
    if args.hold and os.environ.get(HOLD_ENV) != args.hold:
        return rerun_under_hold(args.hold, args.hold_wait, sys.argv[1:] if argv is None else argv)
    global COMFY_URL, COMFY_DIR
    if args.url:
        COMFY_URL = args.url.rstrip("/")
    if args.comfy_dir:
        COMFY_DIR = args.comfy_dir.expanduser()

    with _render_signals():
        return _render(args)


def _render(args) -> int:
    try:
        stepping = bool(os.environ.get(TICKET_ENV) and os.environ.get(REST_ENV))
        jobs = load_jobs((stepping and os.environ.get(JOBS_ENV)) or args.jobs)
        comfy_dir()
        if stepping:
            # Inside a ticket that can hand the rest of the batch back: steps, and ask between
            # them. No ticket, or no place to leave the rest: the batch runs as one, as today.
            return run_in_steps(args, jobs)
        results, timed_out = run(jobs, args.timeout, args.verbose)
    except UsageError as e:
        print(f"comfy-render: {e}", file=sys.stderr)
        return EXIT_USAGE

    _report(args.json, results)
    return _exit_code(results, timed_out)


def _clean_rest(tmp: Path) -> None:
    """The rounds' rest files are comfy-render's own hand-overs: they all go with their folder."""
    shutil.rmtree(tmp, ignore_errors=True)


def rerun_under_hold(name: str, wait: float, argv: list[str]) -> int:
    """Run this same command as `vhotseat hold NAME --class C -- comfy-render ARGV`: a ticket,
    so the queue's serving order decides when the batch gets the card. The copy inside sees
    VHOTSEAT_HOLD=NAME and runs the batch in steps; when it has held the card past its class's
    max hold it leaves the rest in $COMFY_RENDER_REST and exits 5, and this queues a fresh ticket
    for that file - behind whoever was waiting - and carries on until a round ends by itself.
    The worst round's code decides. The hold keeps every other workload's gate refusing until
    the round exits, and passes signals on."""
    vhotseat = os.environ.get("VHOTSEAT_BIN") or shutil.which("vhotseat")
    if not vhotseat:
        print("comfy-render: --hold needs the vhotseat command (not on PATH; or set $VHOTSEAT_BIN)",
              file=sys.stderr)
        return EXIT_USAGE
    args = _parser().parse_args(argv)
    me = [sys.executable, "-m", "vhotseat.kit.comfy_render"]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        filter(None, [str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH")]))}
    # A fresh rest file for every round, in one folder this cleans up when it ends.
    tmp = Path(tempfile.mkdtemp(prefix="comfy-render-rest-"))
    jobs_path = args.jobs or args.hold          # `--hold jobs/x.json`: --hold's NAME took the file
    try:
        total = len(load_jobs(jobs_path))
    except (UsageError, OSError, TypeError):
        total = None                            # the round reports a bad file itself
    try:
        worst, rnd, round_env, results = EXIT_OK, 0, {}, []
        while True:
            rnd += 1
            rest, res = tmp / f"rest-{rnd}.json", tmp / f"results-{rnd}.json"
            env_round = {**env, **round_env, REST_ENV: str(rest),
                         **({RESULTS_ENV: str(res)} if args.json else {}),
                         **({TOTAL_ENV: str(total)} if total is not None else {})}
            # The hold needs its full grace to stop and reap the inner renderer first.
            code = run_with_signals([vhotseat, "hold", name, "--class", args.cls,
                                     *(["--as", args.requester] if args.requester else []),
                                     "--wait", str(wait), "--", *me, *argv],
                                    env=env_round, grace_s=STOP_GRACE_S + 3, report_cancellation=True)
            if res.is_file():
                with contextlib.suppress(OSError, ValueError):
                    results += json.loads(res.read_text())
            if code == EXIT_YIELDED and rest.is_file():
                # The run inside held the card too long and left the rest here: queue a new
                # ticket for that file, and remember how the part that ran went.
                worst = max(worst, _rest_code(rest))
                round_env = {JOBS_ENV: str(rest)}
                if total is not None:
                    with contextlib.suppress(OSError, ValueError):
                        round_env[FIRST_ENV] = str(total - len(json.loads(rest.read_text())) + 1)
                continue
            if args.json:
                print(json.dumps(sorted(results, key=lambda r: r.get("job", 0)), indent=1))
            return max(worst, code)
    finally:
        _clean_rest(tmp)

if __name__ == "__main__":
    sys.exit(main())
