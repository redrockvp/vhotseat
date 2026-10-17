class SwitchError(Exception):
    """The GPU couldn't be handed over. The message says why, for people and the event log."""

    workload = None  # set by the arbiter: the workload that couldn't make room


class Busy(SwitchError):
    """A workload is doing work that must not be interrupted."""


class ReleaseFailed(SwitchError):
    """A workload was asked to release its memory but didn't. Restarting it may help.

    `why` says what's wrong, for the restart's log line; `then` is the event-log
    note if a restart fixes it; `done` lists what the release did before failing."""

    def __init__(self, msg: str, *, why: str, then: str, done: list[str] | None = None):
        super().__init__(msg)
        self.why, self.then, self.done = why, then, done or []
