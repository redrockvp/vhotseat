"""The decision log: every switch, hold and gate decision, one JSON object per line."""

import contextlib
import datetime
import json
import os
import tempfile
import threading
from pathlib import Path

from . import lock, pressure, util
from .lock import Locked

# The rows that say a claim is open, and what has been done about it.
CLAIM_ROWS = ("claim", "claim-end", "claim-release", "claim-release-targets",
              "claim-release-waiting")
# The rows that make an ask: `ask` opens the question (one per workload and requester at a
# time) and `ask-answer` closes it, answered by the owner or by nobody answering in time.
ASK_ROWS = ("ask", "ask-answer")
# A question can also be retired without ever being answered, because whoever would answer it is
# gone: `claim-ended` when the claim it was asked about has ended, `claim-restarted` when the claim
# now belongs to a different process than the one the question was asked about. `ask-end` closes the
# ask for the same reason an answer does - so nothing waits on it or shows it - and `closed_by` says
# which of the two it was, so a retired question is never read as a decision nobody made.
ASK_CLOSE_ROW = "ask-end"
# Everything that takes part in what an ask currently is: opened by one, ended by an answer or by a
# close. Reading the log for asks means reading all three; reading it for answers alone is how a
# closed question would go on looking open.
ASK_ALL_ROWS = (*ASK_ROWS, ASK_CLOSE_ROW)
# How long a row waits for another writer to let go before the write is refused: appending is
# milliseconds of file work, so this waits out a writer that is wedged, not one that is busy.
LOCK_WAIT_S = 5.0
LOCK_POLL_S = 0.05


def ask_open(a: dict) -> bool:
    """Whether this ask can still be answered: no answer yet, and not retired without one.

    Both endings matter. An answered ask is settled. An ask closed by `ask-end` has no answer - the
    game it was asked about is gone - and treating it as still open is how a dashboard ends up
    showing, and a waiter waiting on, a question nobody can act on any more."""
    return not a.get("answer") and not a.get("closed_by")


def row_time(e: dict) -> datetime.datetime | None:
    """A row's own time, or None if it can't be read: a row whose timestamp isn't usable isn't
    news about now, and isn't evidence of when something last happened either."""
    try:
        return datetime.datetime.fromisoformat(e["time"])
    except (KeyError, TypeError, ValueError):
        return None


class EventLog:
    def __init__(self, path: Path, keep: int = 500, telemetry=None):
        self.path = path
        self.keep = keep
        # Gets a copy of every event, kept for its retention window rather than `keep` lines.
        self.telemetry = telemetry
        # Which thread holds this log's write turn, so the turn can be taken again in the same
        # thread that holds it (see `turn`). A thread id and not a counter: a second thread of this
        # process is a second writer, and on Linux two of its file descriptors would otherwise
        # reach the OS lock thinking the turn was already ours.
        self._turn_by = None

    @contextlib.contextmanager
    def turn(self, *, what: str = "writing the log"):
        """One writer to this log at a time, in this process and in every other one.

        Appending a row is a read-check-write over the whole file - read the retained lines, trim,
        add one, write it all back through a rename - so two processes that append in the same
        moment each write a file built from the state they read, and one of the two rows is gone
        with nothing saying it was ever written. That is not a lost line of history: the rows are
        how this box knows what it decided, which claims are open, which workloads were already
        reported, and which question the owner has answered. A row lost under a concurrent write is
        vhotseat acting as if a decision it made had never been made.

        The turn is taken with the operating system's lock on a file beside the log, the same two
        primitives `lock.py` and a config save use, and it waits a bounded while: whoever holds it
        is a millisecond away from letting go, and a caller that has to be told "nothing was
        written" is the wedged writer, not the busy one.

        Held by the thread that took it, and re-entrant for that thread: the check-then-write paths
        (`answer_ask` is one) take the turn themselves so their read and their write are the same
        reading of the file, and the `record()` that follows is the same write, not a second turn.
        """
        mine = threading.get_ident()
        if self._turn_by == mine:
            yield
            return
        # The directory before the turn, not only inside the write: the turn's lock file lives
        # beside the log, so a log that has never been written - a box's first question, before
        # anything has had a reason to record a row - has no directory for the lock to sit in, and
        # `record()`'s own mkdir comes too late for it.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with lock.write_turn(self.path, what=what, wait_s=LOCK_WAIT_S, poll_s=LOCK_POLL_S):
            self._turn_by = mine
            try:
                yield
            finally:
                self._turn_by = None

    def record(self, kind: str, **fields) -> None:
        """Append one event; the log is trimmed to the last `keep` lines. The whole read-trim-write
        is inside `turn()`, which is what makes a row survive being written beside another row.

        A row that can't be written because another writer wouldn't let go is said out loud and
        dropped rather than raised: the decision it records has already happened, and a handover or
        a watcher killed by a log that is busy would be worse than a row the owner can see is
        missing from the log line it prints."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {"time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "event": kind, **fields}
        try:
            with self.turn():
                self._append(event)
        except Locked as e:
            util.log(f"event log: {kind} row not written: {e}")
            return

    def _append(self, event: dict) -> None:
        """The write itself, taken with the turn held: the retained lines plus this one, into a
        temp file named for this write, renamed over the log.

        The name is unique because a shared one is a second way for two writers to collide: on
        Windows the loser of that race gets a `PermissionError` out of the rename and its row is
        lost even when the lock below it didn't fail. A temp file left behind by a write that
        failed is removed; a log is not a place for litter with a name like the log's.

        This is the only place a row is written, so it is also the only place a row is handed to the
        durable history (`telemetry.db`, which keeps decisions far past the log's last 500 lines).
        Handing it over in `record()` instead - the plain-append call - is how the asks went
        missing from the history: an `ask` and the `ask-answer` that closes it are written by the
        check-then-write path, which never passed the copy. The question the owner was asked, and
        what they said, then existed only while the log retained them, and the history - the thing
        that says which unload was a person's decision - never had them. Copying here means a row
        reaches the history exactly when it reaches the log, and a row a wedged writer failed to
        write is in neither."""
        lines = self._read() if self.path.exists() else []
        lines = [*lines[-(self.keep - 1):], json.dumps(event)]
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name + ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            util.replace(Path(tmp), self.path)
        finally:
            with contextlib.suppress(OSError):
                Path(tmp).unlink(missing_ok=True)
        if self.telemetry is not None:
            self.telemetry.event(event)       # reached only once the row has actually landed

    def _read(self) -> list[str]:
        """The log's lines, retried the way `util.replace` retries the write beside it. On Windows
        the rename that appends a row leaves the path briefly unopenable by anyone else, and the
        asking makes that a everyday thing: a request waiting for an answer re-reads the log about
        twice a second, and so do the watchers, while whoever answers writes to it. Losing one of
        those reads to the write it crossed isn't information about the world, so it is tried
        again; the last attempt raises, because a log that can never be read is a real error. The
        wait is a fraction of the poll it happens in, never a whole one."""
        for attempt in range(20):
            try:
                return self.path.read_text(encoding="utf-8").splitlines()
            except PermissionError:
                if attempt == 19:
                    raise
                util.sleep(0.05)
        return []                                            # unreachable: the loop raises or returns

    def tail(self, limit: int = 200) -> list[dict]:
        """The last `limit` events, oldest first, skipping anything unreadable. A reader that
        wants the state of the world as recorded - which claims are still open, which ask is
        unanswered - reads this instead of keeping its own copy of it."""
        if not self.path.exists():
            return []
        out = []
        for line in self._read()[-limit:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    def claims_open(self, limit: int = 500) -> dict:
        """What the log shows as claimed right now: {(process, rule note): {claim, targets,
        kept, released, waiting, freed_mib}}, rebuilt from its tail.

        The log is the record, so it is what says whether a claim has been written, which
        workloads it was owed (`targets`), what has already been released for it, and what has
        already been reported waiting and how many times - rather than each process keeping its
        own copy of that. The trade is that a claim older than the retained log (`keep` lines)
        is forgotten, and recording it again is honest."""
        out: dict[tuple, dict] = {}

        def record(key):
            return out.setdefault(key, {"claim": {"name": key[0], "note": key[1]}, "targets": None,
                                        "kept": [], "released": [], "waiting": [], "freed_mib": 0})

        for e in self.tail(limit):
            kind = e.get("event")
            if kind not in CLAIM_ROWS:
                continue
            key = (e.get("name"), e.get("note"))
            if kind == "claim-end":
                out.pop(key, None)
            elif kind == "claim":
                record(key)["claim"] = e
            elif kind == "claim-release-targets":
                # Decided once, when the claim's release first ran. An empty list is a real
                # answer - nothing of ours was on the card then - so it isn't left as None.
                # `kept` names the workloads that were on the card and are not owed to this
                # claim because of their `yields_to:`: they keep their model, and the game is
                # short of it.
                record(key)["targets"] = list(e.get("targets") or [])
                record(key)["kept"] = list(e.get("kept") or [])
            elif kind == "claim-release":
                rec = record(key)
                got = set(e.get("workloads") or [])
                rec["released"] = sorted(set(rec["released"]) | got)
                rec["freed"] = (rec.get("freed") or []) + (e.get("freed") or [])
                rec["freed_mib"] += e.get("freed_mib") or 0
                # A retry that worked: the row saying it was still holding is history now.
                rec["waiting"] = [w for w in rec["waiting"] if w["workload"] not in got]
            elif e.get("workload"):
                rec = record(key)
                for w in rec["waiting"]:
                    if w["workload"] == e["workload"]:
                        # The newest reason, and however many tries the log has seen.
                        w["why"], w["stuck"] = e.get("why") or w["why"], bool(e.get("stuck")) or w["stuck"]
                        w["attempts"] = max(w.get("attempts") or 0, e.get("attempts") or 1)
                        w["held_mib"] = e.get("held_mib") or w.get("held_mib") or 0
                        break
                else:
                    rec["waiting"].append({"workload": e["workload"], "why": e.get("why") or "",
                                           "attempts": e.get("attempts") or 1,
                                           "stuck": bool(e.get("stuck")),
                                           "held_mib": e.get("held_mib") or 0})
        return out

    def asks(self, limit: int = 500) -> list[dict]:
        """Every ask the retained log holds, oldest first, each with its `answer` and `by` once it
        has one. Same shape of answer as `claims_open` and `episode_open`, for the same reason:
        `watch`, the dashboard, a gate in a fresh process and a toast's button all need the one
        open ask for a (workload, requester) pair, and whichever of them wrote it doesn't get a
        private copy of it. An ask older than the retained log is forgotten, and asking again is
        honest.

        A second `ask-answer` for the same ask - the owner clicking Keep a moment before a
        watcher gives up and applies the fallback - is ignored rather than argued about: the
        first answer in the log is the answer."""
        out: dict[str, dict] = {}
        order: list[str] = []
        for e in self.tail(limit):
            kind = e.get("event")
            if kind not in ASK_ALL_ROWS:
                continue
            ask_id = e.get("id")
            if not ask_id:
                continue
            if kind == "ask":
                if ask_id not in out:
                    out[ask_id] = dict(e)
                    order.append(ask_id)
            elif kind == ASK_CLOSE_ROW:
                if ask_id in out and ask_open(out[ask_id]):
                    out[ask_id].update(closed_by=e.get("closed_by") or "closed",
                                       closed_reason=e.get("reason"), closed_at=e.get("time"))
            elif ask_id in out and ask_open(out[ask_id]):
                # The first ending wins, answer or close: the owner clicking Keep a moment before a
                # watcher gives up on the fallback is the same shape as clicking it a moment before
                # the claim ends, and `answer_ask` and `end_ask` each refuse to write over the other.
                out[ask_id].update(answer=e.get("answer"), by=e.get("by") or "owner",
                                   answered_at=e.get("time"))
        return [out[i] for i in order]

    def answer_ask(self, ask_id: str, answer: str, by: str) -> tuple[str, dict | None]:
        """Write the answer to an open ask, inside the same turn as the check that it is still open.

        Returns (state, the ask as it reads now), where state is `"ok"` when this answer is the one
        in the log, `"answered"` when a different answer got there first, `"closed"` when the ask was
        retired without an answer - the game it was asked about is gone, so there is nothing left for
        an answer to authorise - `"missing"` when the log holds no such ask, and `"locked"` when
        another writer wouldn't let go - which says nothing was written, instead of two answers both
        being believed.

        The check and the write share the turn because an answer is a decision with a winner. Read
        the log here and write there, and an owner clicking Keep while a watcher gives up and
        applies a fallback Release leaves two `ask-answer` rows with no order between them: the log
        reads the first as the answer, and which of the two that is depends on which writer won a
        rename. Then whoever acts on it - unloading a workload the owner said to keep, or keeping
        one the fallback says to unload - is acting on a row that may not be the answer the box is
        showing. This is one write, so the first answer wins in the file rather than in a reading of
        it.
        """
        try:
            with self.turn(what="answering an ask"):
                ask = self.ask_by_id(ask_id)
                if ask is None:
                    return "missing", None
                if ask.get("answer"):
                    return "answered", ask
                if ask.get("closed_by"):
                    return "closed", ask
                self._append({"time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                              "event": "ask-answer", "id": ask_id, "answer": answer, "by": by,
                              "workload": ask.get("workload"), "requester": ask.get("requester")})
                return "ok", self.ask_by_id(ask_id) or {**ask, "answer": answer, "by": by}
        except Locked:
            return "locked", None

    def end_ask(self, ask_id: str, *, closed_by: str, reason: str) -> tuple[str, dict | None]:
        """Retire an open ask that has nothing left to be answered, inside the same turn as the check
        that it is still open. Same states, and the same reason for the turn, as `answer_ask`.

        A question is asked *about* something: this workload unloading for that game process. When
        the game is gone - or is a different process than the one being asked about - the question is
        not merely unanswered, it is moot, and leaving it open has three costs. The dashboard and
        `status` show the owner a question they cannot act on; a waiter keeps waiting on an answer
        that would authorise an unload nobody wants any more; and when the same rule matches a new
        game process, joining it to the old question means its Unload-now is consent the release path
        refuses by pid, so the click returns 200 and does nothing.

        It is a separate row, and a separate ending, from an answer for a reason: `keep` as the
        fallback of a question nobody answered is already in the log as an answer nobody gave, and
        writing one for a game that walked away would read tomorrow as a decision about that unload.
        An `ask-end` row says the question closed because the thing it was about stopped existing."""
        try:
            with self.turn(what="retiring an ask"):
                ask = self.ask_by_id(ask_id)
                if ask is None:
                    return "missing", None
                if ask.get("answer"):
                    return "answered", ask
                if ask.get("closed_by"):
                    return "closed", ask
                self._append({"time": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                              "event": ASK_CLOSE_ROW, "id": ask_id, "closed_by": closed_by,
                              "reason": reason, "workload": ask.get("workload"),
                              "requester": ask.get("requester"), "name": ask.get("name"),
                              "note": ask.get("note"), "pid": ask.get("pid")})
                return "ok", self.ask_by_id(ask_id) or {**ask, "closed_by": closed_by,
                                                        "closed_reason": reason}
        except Locked:
            return "locked", None

    def open_asks(self, limit: int = 500) -> list[dict]:
        """The asks the log shows as still open: no answer, and not retired without one. What
        `/api/status` shows as the open question, and what an actor joins instead of asking the same
        question twice."""
        return [a for a in self.asks(limit) if ask_open(a)]

    def open_ask(self, workload: str, requester: str, limit: int = 500) -> dict | None:
        """The unanswered ask for this workload and this requester, if the log holds one. The one
        question per pair is what stops a job queue asking the owner the same thing fifty times."""
        return next((a for a in self.open_asks(limit)
                     if a.get("workload") == workload and a.get("requester") == requester), None)

    def ask_by_id(self, ask_id: str, limit: int = 500) -> dict | None:
        """One ask by its id, answered or not: what `POST /api/asks/<id>` answers, and what says it
        was answered already (409) or is not in the log at all (404)."""
        return next((a for a in self.asks(limit) if a.get("id") == ask_id), None)

    def kept_said(self, reason: str, name: str | None = None, note: str | None = None,
                  since: str | None = None, limit: int = 500) -> set[str]:
        """Which workloads have already had their `policy-kept` row written for this reason - this
        claim (by name and note) or this episode (by grower name, at or after `since`).

        The row is what tells the owner a workload is still holding memory someone may be short
        of, so it has to be said once per reason, not once per poll: a watcher that re-checks an
        open claim every 2 s would otherwise bury the log in the same warning, and the dashboard
        banner decides what is news from a row's age."""
        out = set()
        for e in self.tail(limit):
            if e.get("event") != "policy-kept" or e.get("reason") != reason:
                continue
            if reason == "claims" and (e.get("name"), e.get("note")) != (name, note):
                continue
            if reason == "pressure" and e.get("name") != name:
                continue
            when, after = row_time(e), row_time({"time": since}) if since else None
            if after and (when is None or when < after):
                continue
            if e.get("workload"):
                out.add(e["workload"])
        return out

    def recent(self, kinds, within_s: float = 300.0, limit: int = 500) -> list[dict]:
        """Rows of these kinds no older than `within_s`, newest first: what the dashboard
        shows as a warning. The log is the record, so "is this still news" is answered from
        the row's own timestamp rather than from anything a process remembers - which is also
        what means both the dashboard and `watch` see the same warnings, and whichever of
        them wrote the row doesn't get a private copy of it."""
        cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(seconds=within_s)
        out = []
        for e in reversed(self.tail(limit)):
            if e.get("event") not in kinds:
                continue
            when = row_time(e)
            if when is not None and when >= cutoff:
                out.append(e)
        return out

    def episode_open(self, limit: int = 500) -> dict | None:
        """The pressure episode the log shows as open, or None: {episode, targets, released,
        waiting, freed_mib}, rebuilt from its rows.

        Same reason as `claims_open`, and the same shape of answer: the episode is one process's
        growth decided once, the workloads it was owed were chosen then, and what has been released
        and what is still holding come from the rows rather than from the memory of whichever
        process wrote them. So `watch` and the dashboard - which both run this check and both act -
        see one episode, and the one that arrives second finds it already in progress instead of
        starting a second one and unloading something again.

        The trade is the same one: an episode older than the retained log (`keep` lines) is
        forgotten, and the next poll treats the card being short as news.
        """
        out: dict | None = None
        for e in self.tail(limit):
            kind = e.get("event")
            if kind not in pressure.EPISODE_ROWS:
                continue
            if kind == "pressure":
                out = {"episode": e, "targets": list(e.get("targets") or []), "released": [],
                       "waiting": [], "freed_mib": 0}
            elif kind == "pressure-end" and out:
                out = None
            elif kind == "pressure-release" and out:
                got = set(e.get("workloads") or [])
                out["released"] = sorted(set(out["released"]) | got)
                out["freed_mib"] += e.get("freed_mib") or 0
                out["waiting"] = [w for w in out["waiting"] if w["workload"] not in got]
            elif kind == "pressure-release-waiting" and out:
                for w in out["waiting"]:
                    if w["workload"] == e.get("workload"):
                        w["why"], w["stuck"] = e.get("why") or w["why"], bool(e.get("stuck")) or w["stuck"]
                        w["attempts"] = max(w.get("attempts") or 0, e.get("attempts") or 1)
                        w["held_mib"] = e.get("held_mib") or w.get("held_mib") or 0
                        break
                else:
                    out["waiting"].append({"workload": e.get("workload") or "", "why": e.get("why") or "",
                                           "attempts": e.get("attempts") or 1,
                                           "stuck": bool(e.get("stuck")), "held_mib": e.get("held_mib") or 0})
        return out

    def last_episode(self, limit: int = 500) -> dict | None:
        """The newest pressure row of any kind, open or closed. `cooldown_s` is measured from it:
        an episode that ended a minute ago is not a clean slate, because the memory that was
        released has to be reloaded before the card is in the state the next release would fix."""
        for e in reversed(self.tail(limit)):
            if e.get("event") in pressure.EPISODE_ROWS and row_time(e) is not None:
                return e
        return None

    def last(self) -> dict | None:
        if not self.path.exists():
            return None
        lines = self.path.read_text(encoding="utf-8").splitlines()
        try:
            return json.loads(lines[-1]) if lines else None
        except json.JSONDecodeError:
            return None
