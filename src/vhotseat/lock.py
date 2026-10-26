"""The one lock per GPU, and the note its holder leaves in it.

Every switch, hold and gate takes this flock. The holder writes what it's doing
("switch strata", "hold comfy") into the file, so a caller that mustn't wait
for the lock can still decide what to do:

- A gate runs inside a workload's own load, which may be the very load a
  `switch` holding the lock is waiting on (how-it-works §4.1). It never waits;
  it reads the note.
- A `switch X` while a `hold X` owns the lock has nothing to do: the hold
  already put the GPU on X and keeps it there. Waiting would deadlock when the
  held command waits on the switch's caller (a ComfyUI job, whose guard runs
  `switch comfy`).

On Windows the flock is a byte-range lock (msvcrt). Those are mandatory, so it
locks one byte far past the note: the note itself stays readable by everyone.
The same two primitives are what `config_edit` puts around a save of the owner's
config file, where the thing being kept in turn is a read-check-write, not a
handover.
"""

import contextlib
import os
import time
from pathlib import Path

from . import util

# A lock file beside the file whose write is being taken turns on, and how long to wait for it.
# A write of one of these files is milliseconds, so the wait is there for the writer that is
# wedged, not the one that is busy - and a refused write the caller can retry beats a hang.
LOCK_SUFFIX = ".lock"
LOCK_WAIT_S = 5.0
LOCK_POLL_S = 0.05


class Locked(Exception):
    """Another process - or another thread of this one - is mid-write to this file and wouldn't let
    go inside the wait. Nothing was written, which is the point: the alternative is two callers both
    told they succeeded, over one file holding one of the two decisions. Worth trying again: unlike
    a version conflict, there is nothing here to re-read first."""


if os.name == "nt":
    import msvcrt

    LOCK_AT = 2**30   # well past any note; Windows lets a lock lie beyond the end of the file

    def try_lock(f) -> bool:
        os.lseek(f.fileno(), LOCK_AT, os.SEEK_SET)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def unlock(f) -> None:
        os.lseek(f.fileno(), LOCK_AT, os.SEEK_SET)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def try_lock(f) -> bool:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def unlock(f) -> None:
        pass  # closing the file releases a flock


@contextlib.contextmanager
def write_turn(path, *, what: str, wait_s: float | None = None, poll_s: float | None = None):
    """One write to `path` at a time, across processes as well as threads.

    This is the guard for a write that is really a read-check-write: read a file, decide what it
    should hold now, replace it. Two writers that read inside each other's gap each write a file
    built from the state they read; both are told they succeeded, and the file holds one of the two
    decisions. The turn is taken on a lock file beside the one being written - not on that file,
    which the write replaces - with the operating system's lock, because the other writer is often
    another process: the watcher, a second dashboard, a gate in a command someone just typed.

    It waits `wait_s` (the module's `LOCK_WAIT_S` unless the caller says otherwise) rather than
    failing at once, because whoever holds the turn is milliseconds from letting go, and it gives up
    rather than waiting forever, because whoever holds it might be wedged: a caller told nothing was
    written can try again, and a caller that hangs cannot. When it raises `Locked` nothing was
    written. The lock file stays where it is: removing a lock file is the race this closes.
    """
    target = Path(path)
    wait = LOCK_WAIT_S if wait_s is None else wait_s
    poll = LOCK_POLL_S if poll_s is None else poll_s
    f = open(target.with_name(target.name + LOCK_SUFFIX), "a+", encoding="utf-8")
    try:
        deadline = time.monotonic() + wait
        while not try_lock(f):
            if time.monotonic() >= deadline:
                raise Locked(f"{target.name}: something else is still {what} - nothing was written")
            util.sleep(poll)
    except BaseException:
        # Not ours: close without touching the range, which belongs to whoever has it - on
        # Windows, unlocking a range you don't hold is an error of its own.
        f.close()
        raise
    try:
        yield
    finally:
        unlock(f)
        f.close()


class Held:
    """The lock, taken. Leaving the `with` block clears the note and releases it."""

    def __init__(self, f, holder: str):
        self._f = f
        self.mark(holder)

    def mark(self, holder: str) -> None:
        self._f.seek(0)
        self._f.truncate()
        self._f.write(holder)
        self._f.flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.mark("")
            unlock(self._f)
        finally:
            self._f.close()


class GpuLock:
    def __init__(self, path: Path):
        self.path = path
        # Bumped by every handover (see bump()); read without the lock.
        self.generation_path = path.with_name(path.stem + ".generation")

    def generation(self) -> int:
        """How many handovers this GPU has seen. A long-running gate (a proxy)
        caches "my workload owns the GPU" together with this number and skips the
        lock while it's unchanged: one small file read per request, no nvidia-smi,
        no HTTP. Any switch, hold or gate that makes room bumps it."""
        try:
            return int(self.generation_path.read_text() or 0)
        except (OSError, ValueError):
            return 0

    def bump(self) -> int:
        """Called with the lock held, after the GPU changed hands (or might have)."""
        n = self.generation() + 1
        tmp = self.generation_path.with_suffix(".tmp")
        tmp.write_text(str(n))
        util.replace(tmp, self.generation_path)
        return n

    def holder(self) -> str:
        """What the current holder is doing ("switch comfy", "hold comfy"), or ""
        when the lock is free (releasing it clears the note). A plain read: the
        lock isn't touched."""
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def try_take(self, holder: str) -> Held | str:
        """Take the lock if it's free; otherwise return the current holder's note."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+", encoding="utf-8")
        if not try_lock(f):
            f.seek(0)
            current = f.read().strip()
            f.close()
            return current
        return Held(f, holder)

    def take(self, holder: str, *, unless=None, on_wait=None, poll: float = 0.2) -> Held | None:
        """Wait for the lock. Returns None instead if, while waiting, `unless`
        is true of the current holder's note."""
        waiting = False
        while True:
            got = self.try_take(holder)
            if isinstance(got, Held):
                return got
            if unless is not None and unless(got):
                return None
            if not waiting and on_wait is not None:
                on_wait(got)
                waiting = True
            util.sleep(poll)
