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


class PolicyRefused(SwitchError):
    """A workload that is holding the card won't give way for this newcomer, so nothing
    can make room for it. `workloads` names the holders, which is what the refusal shown to
    the owner and the `policy-refused` row are built from.

    Only a load is refused this way. It is the newcomer's room that isn't there, not a debt
    someone's memory owes: freeing the card is never refused by a policy any more than by a
    claim (docs/reference.md, "who gives way for whom")."""

    def __init__(self, msg: str, *, workloads: list[str]):
        super().__init__(msg)
        self.workloads = workloads
