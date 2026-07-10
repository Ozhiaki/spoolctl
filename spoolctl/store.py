"""All SQLite access: schema creation, connection pragmas, every query.

No other module touches the database. All coordination between concurrent
spoolctl processes happens through this file's queries; timestamps are
time.time() epoch floats throughout.
"""

from __future__ import annotations

import json
import os
import sqlite3

from spoolctl.models import (
    Attempt,
    BUSY_TIMEOUT_MS,
    Job,
    SCHEMA_VERSION,
    backoff_seconds,
)

DEFAULT_DB_RELPATH = os.path.join(".spoolctl", "queue.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  argv_json       TEXT    NOT NULL,
  state           TEXT    NOT NULL CHECK (state IN
                    ('queued','running','done','failed','dead')),
  attempts        INTEGER NOT NULL DEFAULT 0,
  max_retries     INTEGER NOT NULL DEFAULT 3,
  timeout_seconds INTEGER NOT NULL DEFAULT 300,
  created_at      REAL    NOT NULL,
  next_run_at     REAL    NOT NULL,
  locked_by       TEXT,
  locked_pid      INTEGER,
  locked_at       REAL,
  heartbeat_at    REAL,
  started_at      REAL,
  finished_at     REAL,
  last_exit_code  INTEGER,
  last_error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_claimable ON jobs (state, next_run_at);

CREATE TABLE IF NOT EXISTS attempts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES jobs(id),
  attempt_no  INTEGER NOT NULL,
  worker_id   TEXT    NOT NULL,
  worker_pid  INTEGER NOT NULL,
  state       TEXT    NOT NULL CHECK (state IN
                ('running','succeeded','failed','timed_out','abandoned')),
  started_at  REAL    NOT NULL,
  finished_at REAL,
  exit_code   INTEGER,
  stdout_path TEXT    NOT NULL,
  stderr_path TEXT    NOT NULL,
  error       TEXT,
  UNIQUE (job_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS job_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id    INTEGER NOT NULL REFERENCES jobs(id),
  at        REAL    NOT NULL,
  event     TEXT    NOT NULL,
  worker_id TEXT,
  detail    TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class SchemaTooNewError(Exception):
    """The database was created by a newer spoolctl; upgrading is the fix."""

    def __init__(self, found: int):
        self.found = found
        super().__init__(
            f"queue database schema is version {found}, but this spoolctl "
            f"understands version {SCHEMA_VERSION}; upgrade spoolctl"
        )


def resolve_db_path(flag_value: str | None = None) -> str:
    """--db flag > SPOOLCTL_DB env > ./.spoolctl/queue.db, canonicalized.

    realpath resolves symlinked spellings (macOS /var vs /private/var) so two
    names for one file are one queue.
    """
    path = flag_value or os.environ.get("SPOOLCTL_DB") or DEFAULT_DB_RELPATH
    return os.path.realpath(path)


def output_root(db_path: str) -> str:
    """Directory that holds per-job/per-attempt captured output files."""
    return os.path.join(os.path.dirname(db_path), "output")


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the queue database with the required pragmas.

    Auto-creates the parent directory. Raises SchemaTooNewError when the file
    was written by a newer spoolctl.
    """
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    row = None
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        pass  # meta table absent: fresh database
    if row is not None:
        found = int(row["value"])
        if found > SCHEMA_VERSION:
            raise SchemaTooNewError(found)
        if found == SCHEMA_VERSION:
            return
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


def job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        argv=json.loads(row["argv_json"]),
        state=row["state"],
        attempts=row["attempts"],
        max_retries=row["max_retries"],
        timeout_seconds=row["timeout_seconds"],
        created_at=row["created_at"],
        next_run_at=row["next_run_at"],
        locked_by=row["locked_by"],
        locked_pid=row["locked_pid"],
        locked_at=row["locked_at"],
        heartbeat_at=row["heartbeat_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        last_exit_code=row["last_exit_code"],
        last_error=row["last_error"],
    )


def attempt_from_row(row: sqlite3.Row) -> Attempt:
    return Attempt(
        id=row["id"],
        job_id=row["job_id"],
        attempt_no=row["attempt_no"],
        worker_id=row["worker_id"],
        worker_pid=row["worker_pid"],
        state=row["state"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        exit_code=row["exit_code"],
        stdout_path=row["stdout_path"],
        stderr_path=row["stderr_path"],
        error=row["error"],
    )


def add_job(
    conn: sqlite3.Connection,
    argv: list[str],
    timeout_seconds: int,
    max_retries: int,
    now: float,
) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO jobs (argv_json, state, max_retries, timeout_seconds,"
            " created_at, next_run_at) VALUES (?,?,?,?,?,?)",
            (json.dumps(argv), "queued", max_retries, timeout_seconds, now, now),
        )
        job_id = cur.lastrowid
        add_event(conn, job_id, now, "added")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return job_id


def claim_next(
    conn: sqlite3.Connection,
    worker_id: str,
    worker_pid: int,
    now: float,
    out_root: str,
) -> tuple[Job, Attempt] | None:
    """Atomically claim the oldest eligible queued job.

    BEGIN IMMEDIATE takes the write lock before the SELECT, so concurrent
    claimants serialize; the loser re-reads and finds the row taken. Returns
    None when nothing is eligible. Never holds the transaction past COMMIT —
    the child process runs entirely outside it.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE state='queued' AND next_run_at <= ?"
            " ORDER BY next_run_at, id LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        job_id = row["id"]
        # Monotonic per-job counter, NOT jobs.attempts+1: a manual retry
        # resets the budget to 0 but history keeps its attempt numbers, so
        # earlier attempts' output files are never clobbered.
        attempt_no = conn.execute(
            "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS n FROM attempts WHERE job_id=?",
            (job_id,),
        ).fetchone()["n"]
        attempt_dir = os.path.join(out_root, str(job_id), str(attempt_no))
        stdout_path = os.path.join(attempt_dir, "stdout")
        stderr_path = os.path.join(attempt_dir, "stderr")
        conn.execute(
            "UPDATE jobs SET state='running', locked_by=?, locked_pid=?,"
            " locked_at=?, heartbeat_at=?, started_at=? WHERE id=?",
            (worker_id, worker_pid, now, now, now, job_id),
        )
        cur = conn.execute(
            "INSERT INTO attempts (job_id, attempt_no, worker_id, worker_pid,"
            " state, started_at, stdout_path, stderr_path)"
            " VALUES (?,?,?,?,'running',?,?,?)",
            (job_id, attempt_no, worker_id, worker_pid, now, stdout_path, stderr_path),
        )
        attempt_id = cur.lastrowid
        add_event(conn, job_id, now, "claimed", worker_id)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    job = get_job(conn, job_id)
    attempt_row = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    return job, attempt_from_row(attempt_row)


_OWNERSHIP_GUARD = (
    " WHERE id=? AND state='running' AND locked_by=? AND locked_pid=?"
)


def record_success(
    conn: sqlite3.Connection,
    job_id: int,
    attempt_id: int,
    worker_id: str,
    worker_pid: int,
    now: float,
) -> str | None:
    """Record a successful attempt. Returns 'done', or None when the row was
    reclaimed out from under this worker (result discarded, DB untouched)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "UPDATE jobs SET state='done', locked_by=NULL, locked_pid=NULL,"
            " locked_at=NULL, heartbeat_at=NULL, finished_at=?,"
            " last_exit_code=0, last_error=NULL" + _OWNERSHIP_GUARD,
            (now, job_id, worker_id, worker_pid),
        )
        if cur.rowcount == 0:
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            "UPDATE attempts SET state='succeeded', finished_at=?, exit_code=0"
            " WHERE id=? AND state='running'",
            (now, attempt_id),
        )
        add_event(conn, job_id, now, "succeeded", worker_id)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return "done"


_FAILURE_EVENTS = {"failed": "failed", "timed_out": "timed_out", "abandoned": "reaped"}


def record_failure(
    conn: sqlite3.Connection,
    job_id: int,
    attempt_id: int,
    worker_id: str,
    worker_pid: int,
    kind: str,
    exit_code: int | None,
    error: str,
    now: float,
) -> str | None:
    """Record a failed/timed-out/abandoned attempt and, in the same
    transaction, either requeue with backoff or dead-letter.

    Returns the job's new state ('queued' or 'dead'), or None when the row
    was reclaimed out from under this worker (result discarded).
    """
    if kind not in _FAILURE_EVENTS:
        raise ValueError(f"unknown failure kind: {kind}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT attempts, max_retries FROM jobs"
            " WHERE id=? AND state='running' AND locked_by=? AND locked_pid=?",
            (job_id, worker_id, worker_pid),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        new_attempts = row["attempts"] + 1
        if new_attempts <= row["max_retries"]:
            new_state = "queued"
            next_run_at = now + backoff_seconds(new_attempts)
        else:
            new_state = "dead"
            next_run_at = now
        conn.execute(
            "UPDATE jobs SET state=?, attempts=?, next_run_at=?, locked_by=NULL,"
            " locked_pid=NULL, locked_at=NULL, heartbeat_at=NULL, finished_at=?,"
            " last_exit_code=?, last_error=?" + _OWNERSHIP_GUARD,
            (new_state, new_attempts, next_run_at, now, exit_code, error,
             job_id, worker_id, worker_pid),
        )
        conn.execute(
            "UPDATE attempts SET state=?, finished_at=?, exit_code=?, error=?"
            " WHERE id=? AND state='running'",
            (kind, now, exit_code, error, attempt_id),
        )
        add_event(conn, job_id, now, _FAILURE_EVENTS[kind], worker_id, error)
        if new_state == "dead":
            add_event(conn, job_id, now, "dead", worker_id)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return new_state


def update_heartbeat(
    conn: sqlite3.Connection,
    job_id: int,
    worker_id: str,
    worker_pid: int,
    now: float,
) -> None:
    """Best-effort ownership-guarded heartbeat; lost updates are harmless."""
    conn.execute(
        "UPDATE jobs SET heartbeat_at=?" + _OWNERSHIP_GUARD,
        (now, job_id, worker_id, worker_pid),
    )


def stale_running_candidates(conn: sqlite3.Connection, cutoff: float) -> list[Job]:
    """Running rows whose heartbeat predates cutoff. Candidates only — the
    caller must positively confirm the owner is dead before reaping."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE state='running' AND heartbeat_at < ?"
        " ORDER BY heartbeat_at, id",
        (cutoff,),
    ).fetchall()
    return [job_from_row(r) for r in rows]


def reap(
    conn: sqlite3.Connection,
    job_id: int,
    locked_pid: int,
    cutoff: float,
    now: float,
    reaper_id: str,
) -> str | None:
    """Reclaim a confirmed-dead worker's job through the normal failure path.

    Re-checks under BEGIN IMMEDIATE that the row is still running, still
    owned by the confirmed-dead pid, and still stale; returns the new state
    ('queued' or 'dead'), or None when the re-check failed.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT attempts, max_retries FROM jobs"
            " WHERE id=? AND state='running' AND locked_pid=? AND heartbeat_at < ?",
            (job_id, locked_pid, cutoff),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        new_attempts = row["attempts"] + 1
        if new_attempts <= row["max_retries"]:
            new_state = "queued"
            next_run_at = now + backoff_seconds(new_attempts)
        else:
            new_state = "dead"
            next_run_at = now
        conn.execute(
            "UPDATE jobs SET state=?, attempts=?, next_run_at=?, locked_by=NULL,"
            " locked_pid=NULL, locked_at=NULL, heartbeat_at=NULL, finished_at=?,"
            " last_exit_code=NULL, last_error='worker died' WHERE id=?",
            (new_state, new_attempts, next_run_at, now, job_id),
        )
        conn.execute(
            "UPDATE attempts SET state='abandoned', finished_at=?, error='worker died'"
            " WHERE job_id=? AND state='running'",
            (now, job_id),
        )
        add_event(conn, job_id, now, "reaped", reaper_id, f"dead worker pid {locked_pid}")
        if new_state == "dead":
            add_event(conn, job_id, now, "dead", reaper_id)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return new_state


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return job_from_row(row) if row else None


def retry_job(conn: sqlite3.Connection, job_id: int, force: bool, now: float) -> tuple[str, list[str] | None]:
    """Manually requeue a job with a fresh retry budget.

    Returns (outcome, argv) where outcome is one of:
    ok | not_found | already_queued | done | running_unforced | raced.
    All checks and writes happen inside one BEGIN IMMEDIATE transaction, so
    a forced retry that races a state change resolves to 'raced', never to
    a clobbered row.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT state, argv_json FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return "not_found", None
        state = row["state"]
        argv = json.loads(row["argv_json"])
        if force:
            if state != "running":
                conn.execute("ROLLBACK")
                return "raced", argv
            conn.execute(
                "UPDATE attempts SET state='abandoned', finished_at=?,"
                " error='force-retried' WHERE job_id=? AND state='running'",
                (now, job_id),
            )
        elif state == "queued":
            conn.execute("ROLLBACK")
            return "already_queued", argv
        elif state == "done":
            conn.execute("ROLLBACK")
            return "done", argv
        elif state == "running":
            conn.execute("ROLLBACK")
            return "running_unforced", argv
        conn.execute(
            "UPDATE jobs SET state='queued', attempts=0, next_run_at=?,"
            " locked_by=NULL, locked_pid=NULL, locked_at=NULL, heartbeat_at=NULL,"
            " finished_at=NULL, last_exit_code=NULL, last_error=NULL WHERE id=?",
            (now, job_id),
        )
        add_event(conn, job_id, now, "retried", None, "forced" if force else None)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return "ok", argv


def state_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Job counts by state, zero-filled for every state, keys sorted."""
    counts = {state: 0 for state in sorted(("queued", "running", "done", "failed", "dead"))}
    for row in conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"):
        counts[row["state"]] = row["n"]
    return counts


def recent_dead(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Most recently finished dead jobs with their latest attempt's paths."""
    rows = conn.execute(
        "SELECT j.id, j.argv_json, j.attempts, j.last_error, j.finished_at,"
        " a.stdout_path, a.stderr_path"
        " FROM jobs j LEFT JOIN attempts a"
        "   ON a.job_id = j.id"
        "   AND a.attempt_no = (SELECT MAX(attempt_no) FROM attempts WHERE job_id = j.id)"
        " WHERE j.state='dead'"
        " ORDER BY j.finished_at DESC, j.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        command = " ".join(json.loads(r["argv_json"]))
        if len(command) > 80:
            command = command[:77] + "..."
        out.append({
            "attempts": r["attempts"],
            "command": command,
            "finished_at": r["finished_at"],
            "id": r["id"],
            "last_error": r["last_error"],
            "stderr_path": r["stderr_path"],
            "stdout_path": r["stdout_path"],
        })
    return out


def add_event(
    conn: sqlite3.Connection,
    job_id: int,
    at: float,
    event: str,
    worker_id: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO job_events (job_id, at, event, worker_id, detail) VALUES (?,?,?,?,?)",
        (job_id, at, event, worker_id, detail),
    )
