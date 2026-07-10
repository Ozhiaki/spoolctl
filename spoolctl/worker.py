"""Claim/execute/record loop, heartbeat thread, reaper, timeout enforcement.

The child's streams are redirected straight to per-attempt files at spawn —
no pipes, so no draining threads, no memory cap, no deadlock. Timeouts kill
the whole process group (start_new_session=True), grandchildren included.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

from spoolctl import store
from spoolctl.models import (
    Attempt,
    HEARTBEAT_INTERVAL,
    Job,
    KILL_GRACE_SECONDS,
    REAP_THRESHOLD,
)

# A live pid whose command line contains this marker is presumed to be a
# spoolctl worker (pid not recycled): do not reap.
WORKER_CMDLINE_MARKER = "spoolctl"


def heartbeat_interval() -> float:
    return float(os.environ.get("SPOOLCTL_TEST_HEARTBEAT_INTERVAL", HEARTBEAT_INTERVAL))


def reap_threshold() -> float:
    return float(os.environ.get("SPOOLCTL_TEST_REAP_THRESHOLD", REAP_THRESHOLD))


def is_worker_pid_dead(pid: int) -> bool:
    """Positively confirm that a lock-owning worker process is dead.

    True only on proof: the pid is gone, or it is alive but its command line
    is not a spoolctl worker (recycled pid). PermissionError, ps failure, or
    anything inconclusive returns False — assume alive, never reap. The cost
    of a wrong False is reaping delay; the cost of a wrong True is double
    execution, which is the one forbidden outcome.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False  # ps couldn't say; the kill(0) probe will settle it next pass
    cmdline = proc.stdout.strip()
    if not cmdline:
        return False
    return WORKER_CMDLINE_MARKER not in cmdline


def reap_pass(conn, now: float | None = None, reaper_id: str = "reaper") -> list[int]:
    """Reap every stale-heartbeat running job whose owner is confirmed dead.

    Staleness only nominates; liveness is checked outside any transaction and
    store.reap re-verifies under BEGIN IMMEDIATE. Returns reaped job ids.
    """
    now = time.time() if now is None else now
    cutoff = now - reap_threshold()
    reaped: list[int] = []
    for job in store.stale_running_candidates(conn, cutoff):
        if job.locked_pid is None or not is_worker_pid_dead(job.locked_pid):
            continue
        state = store.reap(conn, job.id, job.locked_pid, cutoff, now, reaper_id)
        if state is not None:
            print(
                f"spoolctl: reaped job {job.id} (worker pid {job.locked_pid} died);"
                f" now {state}",
                file=sys.stderr,
            )
            reaped.append(job.id)
    return reaped


class Heartbeat:
    """Background thread that refreshes jobs.heartbeat_at while a job runs.

    Uses its own connection (sqlite3 objects are not shared across threads).
    Lost or late updates are harmless: staleness only nominates candidates.
    """

    def __init__(self, db_path: str, job_id: int, worker_id: str, worker_pid: int):
        self._db_path = db_path
        self._job_id = job_id
        self._worker_id = worker_id
        self._worker_pid = worker_pid
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        conn = store.connect(self._db_path)
        try:
            while not self._stop.wait(heartbeat_interval()):
                try:
                    store.update_heartbeat(
                        conn, self._job_id, self._worker_id, self._worker_pid, time.time()
                    )
                except Exception:
                    pass  # e.g. transient busy; the next beat retries
        finally:
            conn.close()

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=heartbeat_interval() + 5)


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
