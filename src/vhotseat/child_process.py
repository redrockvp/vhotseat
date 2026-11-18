"""Run a command to completion, forwarding caller cancellation before escalating."""

import contextlib
import os
import signal
import subprocess
import threading

# Allow comfy-render's five-second cleanup, including time to notice the signal.
STOP_GRACE_S = 8.0


def run_with_signals(command, *, env, grace_s, report_cancellation=False):
    windows = os.name == "nt"
    cancelled = None

    def stop(signum, _frame):
        nonlocal cancelled
        if cancelled is None:
            cancelled = signum

    with contextlib.ExitStack() as restore:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGBREAK if windows else signal.SIGHUP):
                restore.callback(signal.signal, sig, signal.signal(sig, stop))
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0
        with subprocess.Popen(command, env=env, creationflags=flags) as proc:
            while cancelled is None:
                try:
                    # Windows waits on a process handle. Wake Python to dispatch signals.
                    proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
            if proc.returncode is None:
                try:
                    proc.send_signal(signal.CTRL_BREAK_EVENT if windows else cancelled)
                    proc.wait(timeout=grace_s)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()
                    proc.wait()
            # A cancelled wrapper must not requeue a round that concurrently yielded.
            if report_cancellation and cancelled is not None:
                return 128 + cancelled
            return proc.returncode
