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


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return job_from_row(row) if row else None


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
