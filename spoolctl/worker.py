"""Claim/execute/record loop, heartbeat thread, reaper, timeout enforcement.

The child's streams are redirected straight to per-attempt files at spawn —
no pipes, so no draining threads, no memory cap, no deadlock. Timeouts kill
the whole process group (start_new_session=True), grandchildren included.
"""

from __future__ import annotations

import os
import signal
import socket
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

    Cancellation delivery: a *successful* UPDATE that matched zero rows is
    positive proof this worker no longer owns the row (canceled, reaped, or
    force-retried) — on_lost fires exactly once and the thread stops.
    Exceptions (e.g. transient busy) are not proof: swallow and retry,
    never fire on_lost on an exception.
    """

    def __init__(
        self,
        db_path: str,
        job_id: int,
        worker_id: str,
        worker_pid: int,
        on_lost=None,
    ):
        self._db_path = db_path
        self._job_id = job_id
        self._worker_id = worker_id
        self._worker_pid = worker_pid
        self._on_lost = on_lost
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        conn = store.connect(self._db_path)
        try:
            while not self._stop.wait(heartbeat_interval()):
                try:
                    matched = store.update_heartbeat(
                        conn, self._job_id, self._worker_id, self._worker_pid, time.time()
                    )
                except Exception:
                    continue  # e.g. transient busy; the next beat retries
                if matched == 0:
                    if self._on_lost is not None:
                        self._on_lost()
                    return  # ownership is gone; nothing left to beat for
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


def execute_attempt(
    job: Job,
    attempt: Attempt,
    on_spawn=None,
) -> tuple[str, int | None, str | None]:
    """Run one claimed attempt to completion or timeout.

    Returns (kind, exit_code, error) with kind in
    succeeded | failed | timed_out. Spawn failures are ordinary failures:
    the worker loop must survive them. on_spawn (if given) receives the
    Popen right after spawn, so a signal handler can target the group.
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
        if on_spawn is not None:
            on_spawn(proc)
        try:
            exit_code = proc.wait(timeout=job.timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            return "timed_out", None, f"timed out after {job.timeout_seconds}s"
    if exit_code == 0:
        return "succeeded", 0, None
    return "failed", exit_code, f"exit {exit_code}"


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def process_one(
    conn,
    db_path: str,
    worker_id: str,
    lane: str = "default",
    slots: int | None = None,
    on_spawn=None,
) -> dict | None:
    """One claim cycle: reap pass, claim, execute, record.

    Returns a summary dict for the executed job, or None when nothing was
    eligible. Never holds a DB transaction while the child runs.
    """
    reap_pass(conn, reaper_id=worker_id)
    claimed = store.claim_next(
        conn, worker_id, os.getpid(), time.time(), store.output_root(db_path),
        lane=lane, slots=slots,
    )
    if claimed is None:
        return None
    job, attempt = claimed
    started = time.monotonic()
    current: dict = {"proc": None}

    def _on_spawn(proc):
        current["proc"] = proc
        if on_spawn is not None:
            on_spawn(proc)

    def _on_lost():
        # The row was canceled, reaped, or force-retried out from under us:
        # this worker owns the child, so this worker kills it.
        proc = current["proc"]
        if proc is not None and proc.poll() is None:
            print(
                f"spoolctl: job {job.id} ownership lost; killing its process group",
                file=sys.stderr,
            )
            _kill_group(proc)

    with Heartbeat(db_path, job.id, worker_id, os.getpid(), on_lost=_on_lost):
        kind, exit_code, error = execute_attempt(job, attempt, on_spawn=_on_spawn)
    now = time.time()
    if kind == "succeeded":
        new_state = store.record_success(
            conn, job.id, attempt.id, worker_id, os.getpid(), now)
    else:
        new_state = store.record_failure(
            conn, job.id, attempt.id, worker_id, os.getpid(), kind, exit_code, error, now)
    if new_state is None:
        print(
            f"spoolctl: warning: job {job.id} was reclaimed while running;"
            f" discarding stale result ({kind})",
            file=sys.stderr,
        )
    elapsed = time.monotonic() - started
    print(
        f"spoolctl: job {job.id} attempt {attempt.attempt_no} {kind}"
        f" ({elapsed:.1f}s) -> {new_state or 'discarded'}",
        file=sys.stderr,
    )
    return {
        "attempt_no": attempt.attempt_no,
        "job_id": job.id,
        "job_state": new_state,
        "result": kind,
    }


def work_loop(
    db_path: str,
    worker_id: str,
    poll_interval: float,
    drain: bool = False,
    lane: str = "default",
    slots: int | None = None,
) -> dict:
    """Run jobs until SIGINT/SIGTERM — or, when draining, until the queue
    settles (zero queued or running rows at a moment nothing was claimable;
    a job added after that check belongs to the next invocation).

    First signal: finish and record the in-flight job, then exit 0. Second
    signal: SIGKILL the job's process group (recorded as a normal failure),
    then exit 0. SIGKILL of the worker itself is deliberately unhandled —
    the reaper is the recovery path.

    Returns {"drained": bool, "executed": int}: executed counts jobs this
    process ran; drained is True only when a drain finished because the
    queue settled, not because a signal stopped it early."""
    stop = threading.Event()
    current: dict = {"proc": None}
    executed = 0
    settled = False

    def on_signal(signum, frame):
        if stop.is_set():
            proc = current["proc"]
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        else:
            stop.set()
            print("spoolctl: stopping after in-flight job (signal again to kill it)",
                  file=sys.stderr)

    def on_spawn(proc):
        current["proc"] = proc

    old_int = signal.signal(signal.SIGINT, on_signal)
    old_term = signal.signal(signal.SIGTERM, on_signal)
    conn = store.connect(db_path)
    try:
        while not stop.is_set():
            summary = process_one(
                conn, db_path, worker_id, lane=lane, slots=slots, on_spawn=on_spawn
            )
            current["proc"] = None
            if summary is not None:
                executed += 1
                continue
            if drain and store.unsettled_count(conn, lane=lane, now=time.time()) == 0:
                settled = True
                break
            if not stop.is_set():
                stop.wait(poll_interval)
    finally:
        conn.close()
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    return {"drained": drain and settled, "executed": executed}
