"""Claim/execute/record loop, heartbeat thread, reaper, timeout enforcement.

The child's streams are redirected straight to per-attempt files at spawn —
no pipes, so no draining threads, no memory cap, no deadlock. Timeouts kill
the whole process group (start_new_session=True), grandchildren included.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

from spoolctl.models import Attempt, Job, KILL_GRACE_SECONDS


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the child's process group, grace, then SIGKILL; reap the child."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=KILL_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def execute_attempt(job: Job, attempt: Attempt) -> tuple[str, int | None, str | None]:
    """Run one claimed attempt to completion or timeout.

    Returns (kind, exit_code, error) with kind in
    succeeded | failed | timed_out. Spawn failures are ordinary failures:
    the worker loop must survive them.
    """
    os.makedirs(os.path.dirname(attempt.stdout_path), exist_ok=True)
    with open(attempt.stdout_path, "wb") as out_f, open(attempt.stderr_path, "wb") as err_f:
        try:
            proc = subprocess.Popen(
                job.argv,
                stdout=out_f,
                stderr=err_f,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            return "failed", None, f"spawn failed: {exc}"
        try:
            exit_code = proc.wait(timeout=job.timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            return "timed_out", None, f"timed out after {job.timeout_seconds}s"
    if exit_code == 0:
        return "succeeded", 0, None
    return "failed", exit_code, f"exit {exit_code}"
