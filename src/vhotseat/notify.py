"""Asking the owner, in words, wherever an answer can be read back.

An ask is a row in the event log (`events.py`), and the log - not this module - is what says
whether it is open, what its answer was and who gave it. This module is only the two ends of
that row: the sentences the question is asked in, and whatever carries them to a person.

The sentences live here because the notification, the dashboard and `vhotseat status` must ask
the same question in the same words: an owner who is asked one thing in a toast and reads
another on the page answers a question they weren't shown. They are built from the row's own
fields (`workload`, `requester`, `held_mib`, `last_used`, `timeout_s`, `fallback`), so the words
always describe the ask that is open rather than a config that may have changed since.

The carrier is deliberately an interface with a fake: whether a box can notify anybody is not
something the asking may depend on. A `watch` at logon over a disconnected RDP session, a
Linux box, a machine without the Windows toast library installed: the question is still in the
log, still on the dashboard, still answerable, and `/api/status` says notifications are
unavailable rather than pretending an answer is on its way.

Nothing here opens or answers an ask - the arbiter does both - and no notification may use
protocol or URL activation: a toast that opens a browser is worse than no toast at all when the
reason for the question is a game filling the screen. Buttons call back into the process that
showed them, which answers the ask through the same path the dashboard's button uses.
"""

from __future__ import annotations

import os
from typing import Callable

from . import policy

# The two answers, as they go in the `ask-answer` row, and as a toast labels them.
RELEASE, KEEP = policy.ANSWERS


def gib_text(mib) -> str:
    """MiB as the person reads a card: "21.1 GB", "900 MB". Nobody compares a MiB figure to the
    size of the card in their head; the MiB stay in the row, where the arithmetic happens."""
    if mib is None:
        return "an unknown amount of memory"
    if mib < 1024:
        return f"{int(mib)} MB"
    return f"{mib / 1024:.1f} GB"


def ask_text(ask: dict) -> dict:
    """The question in words, from the `ask` row.

    Returns `headline` (what the pair is called), `title` (the notification's line), `detail`
    (what is holding what, when it was last seen working, and what the newcomer is doing while
    the owner decides), `no_answer` (what happens if nobody answers, which is the part of an Ask
    me an owner is otherwise choosing blind), and the two answers twice labelled: `buttons` short
    enough for a notification whose title already names the model, and `answers` for the page,
    where the heading is the question and so the buttons have to say which model they mean.

    Every figure comes out of the row: the memory it holds, `last_used` (said as unknown when
    nothing recorded it, never as idle), and the newcomer's name, because "a game started" is a
    question about a specific game.
    """
    label = ask.get("label") or ask.get("workload") or "the workload"
    requester = ask.get("requester")
    held = gib_text(ask.get("held_mib"))
    used = policy.used_phrase(ask.get("ago_s"))
    who = ask.get("requester_label") or requester or "the newcomer"
    name = ask.get("name")
    free = gib_text(ask.get("free_mib")) if ask.get("free_mib") is not None else None

    if requester == policy.CLAIMS:
        headline = "A game wants the GPU"
        title = f"A game started: {name}" if name else headline
        meanwhile = (f"The game runs with {free} free meanwhile"
                     if free else "The game runs with whatever is free meanwhile")
        detail = (f"{label} is holding {held} and {used}. {meanwhile}, so pause your work if you "
                  f"need to, then decide: unload {label} to give the game the GPU?")
    elif requester == policy.PRESSURE:
        headline = "The card is running short"
        title = f"Another app took the card: {name}" if name else headline
        grower = name or "An app vhotseat doesn't manage"
        detail = (f"{label} is holding {held} and {used}. {grower} is short of VRAM now and "
                  f"nothing else is being unloaded for it. Unload {label} to free room for it?")
    else:
        headline = f"{who} wants the GPU"
        title = headline
        detail = (f"{label} is holding {held} and {used}. {who} is waiting for your answer rather "
                  f"than being refused or unloading anything. Unload {label} so it can load?")

    fallback_line = (f"{label} stays loaded" if ask.get("fallback", "keep") == "keep"
                     else f"{label} unloads")
    return {
        "headline": headline,
        "title": title,
        "detail": detail,
        "used": used,
        "buttons": {RELEASE: "Unload now", KEEP: f"Keep {label}"},
        "answers": [{"answer": RELEASE, "label": f"Unload {label} now"},
                    {"answer": KEEP, "label": f"Keep {label} loaded"}],
        "no_answer": (f"No answer in {policy.wait_phrase(ask.get('timeout_s') or 0)}: "
                      f"{fallback_line}."),
    }


class Notifier:
    """Whatever can put a question in front of a person. Subclasses never raise: a box that
    cannot notify still has the ask in its log and on its dashboard."""

    name = "none"

    def status(self) -> dict:
        """Whether this box can notify anybody, and if not, why - shown in `/api/status` so the
        owner is not left waiting for a popup that was never going to arrive."""
        return {"available": False, "how": self.name,
                "why": "no notifications are set up on this process"}

    def show_ask(self, ask: dict, on_answer: Callable[[str, str], object]) -> bool:
        """Show the question. `on_answer(ask_id, answer)` is what its buttons call, which is the
        same answer path the dashboard's button uses. True if it got shown."""
        return False


class NullNotifier(Notifier):
    """The fake and the fallback: shows nothing, says so, and lets the log and the dashboard be
    the question."""

    def __init__(self, why: str = "notifications are off"):
        self.why = why

    def status(self) -> dict:
        return {"available": False, "how": self.name, "why": self.why}


class WindowsToastsNotifier(Notifier):
    """A Windows toast with two buttons, answered by this process.

    `windows-toasts`' `InteractableWindowsToaster` gives the buttons an `on_activated` callback,
    which is what makes the answer come back here rather than to a protocol handler: opening a
    browser, or any other app, over a fullscreen game is worse than showing nothing at all. That
    also means the toast is only live while the process that raised it lives - the ask outlives
    it, in the log, and the dashboard can still answer it.

    Constructing the toaster is what fails on a machine with no desktop session for this process,
    so it happens on the first ask rather than at startup, and the reason is remembered: `status`
    reports it, and the ask stays where it can still be answered.
    """

    name = "windows-toasts"

    def __init__(self, app_name: str = "vhotseat"):
        self.app_name = app_name
        self._toaster = None
        self._failed: str | None = None
        # The Toast objects, kept referenced while their buttons may still be clicked: the
        # activation callback is a closure over the ask, and windows-toasts holds no reference
        # of its own that outlives show_toast().
        self._shown: list = []

    def status(self) -> dict:
        if self._failed:
            return {"available": False, "how": self.name, "why": self._failed}
        return {"available": True, "how": self.name, "why": None}

    def _setup(self):
        if self._toaster is None:
            from windows_toasts import InteractableWindowsToaster
            self._toaster = InteractableWindowsToaster(self.app_name)
        return self._toaster

    def show_ask(self, ask: dict, on_answer) -> bool:
        try:
            from windows_toasts import Toast, ToastButton, ToastDuration
            toaster = self._setup()
            text = ask_text(ask)
            ask_id = ask.get("id") or ""

            def activated(event):
                # Only a button's answer counts: clicking the toast itself arrives with no
                # arguments, and guessing which button was meant is how a question gets
                # answered the wrong way round.
                arguments = (getattr(event, "arguments", "") or "").split(":")
                answer = next((a for a in reversed(arguments) if a in (RELEASE, KEEP)), None)
                if answer and ask_id:
                    on_answer(ask_id, answer)

            toast = Toast(text_fields=[text["title"], text["detail"], text["no_answer"]],
                          duration=ToastDuration.Long, on_activated=activated,
                          group="vhotseat-ask")
            toast.tag = f"vhotseat-ask-{ask_id}"
            toast.AddAction(ToastButton(text["buttons"][RELEASE], f"{ask_id}:{RELEASE}"))
            toast.AddAction(ToastButton(text["buttons"][KEEP], f"{ask_id}:{KEEP}"))
            toaster.show_toast(toast)
            self._shown = (self._shown + [toast])[-8:]
            return True
        except Exception as e:                              # noqa: BLE001 - a toast may not break the ask
            # Named once, and the ask is unaffected: the log and the dashboard still hold the
            # question, and `/api/status` now says notifications aren't happening here.
            self._failed = f"{type(e).__name__}: {e}"
            self._toaster = None
            return False


def build(env=os.environ) -> Notifier:
    """The notifier for this process: toasts on Windows, nothing elsewhere.

    Cheap and import-free on every path that isn't showing a toast, so an arbiter built for a
    test, a Linux box or a headless service has never touched the notification stack. A process
    that wants a different one (a test with a recording fake, a tray app with its own UI) passes
    it to the arbiter instead.
    """
    if os.name != "nt" or env.get("VHOTSEAT_NOTIFY", "1") == "0":
        return NullNotifier("notifications are off on this platform" if os.name != "nt"
                            else "VHOTSEAT_NOTIFY=0")
    return WindowsToastsNotifier()
