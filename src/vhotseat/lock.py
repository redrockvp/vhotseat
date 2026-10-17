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
"""

import fcntl
from pathlib import Path

from . import util


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
        finally:
            self._f.close()


class GpuLock:
    def __init__(self, path: Path):
        self.path = path

    def try_take(self, holder: str) -> Held | str:
        """Take the lock if it's free; otherwise return the current holder's note."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
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
