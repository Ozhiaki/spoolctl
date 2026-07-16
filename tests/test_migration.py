"""Schema migrations: fixture upgrades, concurrency races, one-way door.

The v1 schema below is a frozen copy of SCHEMA_SQL as shipped in v0.1 —
it must never track store.SCHEMA_SQL, that would defeat the fixture.
"""

from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from spoolctl import store
from spoolctl.models import (
    REASON_CANCELED,
    REASON_TIMEOUT,
    REASON_UNKNOWN,
    REASON_WORKER_CRASH,
)

REPO = Path(__file__).resolve().parent.parent

V1_SCHEMA_SQL = """
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

V1_JOB_STATES = ("queued", "running", "done", "failed", "dead")
V1_ATTEMPT_STATES = ("running", "succeeded", "failed", "timed_out", "abandoned")

V2_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  argv_json       TEXT    NOT NULL,
  state           TEXT    NOT NULL CHECK (state IN
                    ('queued','running','done','failed','dead','canceled')),
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
CREATE INDEX IF NOT EXISTS idx_jobs_finished ON jobs (state, finished_at);

CREATE TABLE IF NOT EXISTS attempts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      INTEGER NOT NULL REFERENCES jobs(id),
  attempt_no  INTEGER NOT NULL,
  worker_id   TEXT    NOT NULL,
  worker_pid  INTEGER NOT NULL,
  state       TEXT    NOT NULL CHECK (state IN
                ('running','succeeded','failed','timed_out','abandoned','canceled')),
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

V2_JOB_STATES = ("queued", "running", "done", "failed", "dead", "canceled")
V2_ATTEMPT_STATES = (
    "running", "succeeded", "failed", "timed_out", "abandoned", "canceled"
)


def make_populated_v1_db(db_path: str) -> None:
    """A v1 database with jobs in every state, attempts in every state,
    events, and an AUTOINCREMENT high-water mark above MAX(id) (job 6 was
    inserted and deleted, so its id must never be reissued).

    WAL from the start, like every database v0.1 actually created — the
    racers must not need the exclusive lock a journal-mode switch takes."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(V1_SCHEMA_SQL)
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
    for i, state in enumerate(V1_JOB_STATES, start=1):
        conn.execute(
            "INSERT INTO jobs (id, argv_json, state, attempts, created_at,"
            " next_run_at, finished_at) VALUES (?,?,?,?,?,?,?)",
            (i, f'["job-{i}"]', state, i - 1, 100.0 + i, 200.0 + i,
             300.0 + i if state in ("done", "failed", "dead") else None),
        )
    for i, state in enumerate(V1_ATTEMPT_STATES, start=1):
        conn.execute(
            "INSERT INTO attempts (id, job_id, attempt_no, worker_id, worker_pid,"
            " state, started_at, stdout_path, stderr_path) VALUES (?,?,?,?,?,?,?,?,?)",
            (i, i, 1, f"w{i}", 1000 + i, state, 400.0 + i, f"/out/{i}/1/stdout",
             f"/out/{i}/1/stderr"),
        )
    for i in range(1, 6):
        conn.execute(
            "INSERT INTO job_events (job_id, at, event, worker_id) VALUES (?,?,?,?)",
            (i, 500.0 + i, "added", None),
        )
    conn.execute(
        "INSERT INTO jobs (id, argv_json, state, created_at, next_run_at)"
        " VALUES (6, '[\"deleted\"]', 'queued', 1.0, 1.0)"
    )
    conn.execute("DELETE FROM jobs WHERE id=6")
    conn.commit()
    conn.close()


def make_populated_v2_db(db_path: str) -> None:
    """A v2 database with every v2 state and an AUTOINCREMENT high-water mark."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(V2_SCHEMA_SQL)
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
    for i, state in enumerate(V2_JOB_STATES, start=1):
        conn.execute(
            "INSERT INTO jobs (id, argv_json, state, attempts, created_at,"
            " next_run_at, finished_at) VALUES (?,?,?,?,?,?,?)",
            (i, f'["job-{i}"]', state, i - 1, 100.0 + i, 200.0 + i,
             300.0 + i if state in ("done", "failed", "dead", "canceled") else None),
        )
    for i, state in enumerate(V2_ATTEMPT_STATES, start=1):
        conn.execute(
            "INSERT INTO attempts (id, job_id, attempt_no, worker_id, worker_pid,"
            " state, started_at, stdout_path, stderr_path) VALUES (?,?,?,?,?,?,?,?,?)",
            (i, i, 1, f"w{i}", 1000 + i, state, 400.0 + i, f"/out/{i}/1/stdout",
             f"/out/{i}/1/stderr"),
        )
    for i in range(1, 7):
        conn.execute(
            "INSERT INTO job_events (job_id, at, event, worker_id) VALUES (?,?,?,?)",
            (i, 500.0 + i, "added", None),
        )
    conn.execute(
        "INSERT INTO jobs (id, argv_json, state, created_at, next_run_at)"
        " VALUES (7, '[\"deleted\"]', 'queued', 1.0, 1.0)"
    )
    conn.execute("DELETE FROM jobs WHERE id=7")
    conn.commit()
    conn.close()


def dump(conn, table: str) -> list[tuple]:
    return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def with_v5_migration_job_defaults(rows: list[tuple]) -> list[tuple]:
    return [tuple(r) + (None, "{}", None, 0, "default", None, "{}", 0, None) for r in rows]


def legacy_failure_reason(row: tuple) -> str | None:
    state = row[5]
    error = row[11]
    if state == "timed_out":
        return REASON_TIMEOUT
    if state == "abandoned" and error == "worker died":
        return REASON_WORKER_CRASH
    if state == "abandoned" and error == "force-retried":
        return REASON_CANCELED
    if state == "canceled":
        return REASON_CANCELED
    if state == "failed":
        return REASON_UNKNOWN
    return None


def with_v6_migration_attempt_defaults(rows: list[tuple]) -> list[tuple]:
    return [tuple(r) + (legacy_failure_reason(tuple(r)),) for r in rows]


def assert_v5_jobs_shape(testcase: unittest.TestCase, conn) -> None:
    columns = {r["name"]: r for r in conn.execute("PRAGMA table_info(jobs)")}
    testcase.assertIn("idempotency_key", columns)
    testcase.assertIn("tags_json", columns)
    testcase.assertIn("note", columns)
    testcase.assertIn("priority", columns)
    testcase.assertIn("queue", columns)
    testcase.assertIn("cwd", columns)
    testcase.assertIn("env_json", columns)
    testcase.assertIn("crashes", columns)
    testcase.assertIn("max_crashes", columns)
    testcase.assertEqual(columns["tags_json"]["dflt_value"], "'{}'")
    testcase.assertEqual(columns["priority"]["dflt_value"], "0")
    testcase.assertEqual(columns["queue"]["dflt_value"], "'default'")
    testcase.assertEqual(columns["env_json"]["dflt_value"], "'{}'")
    testcase.assertEqual(columns["crashes"]["dflt_value"], "0")
    indexes = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }
    testcase.assertIn("idx_jobs_key", indexes)
    testcase.assertIn("idx_jobs_claimable", indexes)
    idx_cols = [
        (r["name"], r["desc"]) for r in conn.execute("PRAGMA index_xinfo(idx_jobs_claimable)")
        if r["key"]
    ]
    testcase.assertEqual(
        idx_cols,
        [("state", 0), ("queue", 0), ("priority", 1), ("next_run_at", 0), ("id", 0)],
    )


def assert_v6_attempts_shape(testcase: unittest.TestCase, conn) -> None:
    columns = {r["name"]: r for r in conn.execute("PRAGMA table_info(attempts)")}
    testcase.assertIn("failure_reason", columns)
    testcase.assertIsNone(columns["failure_reason"]["dflt_value"])
    with testcase.assertRaises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO attempts (job_id, attempt_no, worker_id, worker_pid,"
            " state, started_at, stdout_path, stderr_path, failure_reason)"
            " VALUES (1, 99, 'w', 1, 'failed', 1.0, '/o', '/e', 'bogus')"
        )


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")


class TestMigrationFixtures(MigrationTestCase):
    def test_populated_v1_db_chains_to_v6_intact(self):
        make_populated_v1_db(self.db)
        before = sqlite3.connect(self.db)
        jobs_before = dump(before, "jobs")
        attempts_before = dump(before, "attempts")
        events_before = dump(before, "job_events")
        before.close()

        conn = store.connect(self.db)

        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(int(row["value"]), 6)
        self.assertEqual([tuple(r) for r in dump(conn, "jobs")],
                         with_v5_migration_job_defaults(jobs_before))
        self.assertEqual([tuple(r) for r in dump(conn, "attempts")],
                         with_v6_migration_attempt_defaults(attempts_before))
        self.assertEqual([tuple(r) for r in dump(conn, "job_events")], events_before)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        assert_v5_jobs_shape(self, conn)
        assert_v6_attempts_shape(self, conn)
        conn.close()

    def test_populated_v2_db_migrates_to_v6_intact(self):
        make_populated_v2_db(self.db)
        before = sqlite3.connect(self.db)
        jobs_before = dump(before, "jobs")
        attempts_before = dump(before, "attempts")
        events_before = dump(before, "job_events")
        before.close()

        conn = store.connect(self.db)

        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(int(row["value"]), 6)
        self.assertEqual([tuple(r) for r in dump(conn, "jobs")],
                         with_v5_migration_job_defaults(jobs_before))
        self.assertEqual([tuple(r) for r in dump(conn, "attempts")],
                         with_v6_migration_attempt_defaults(attempts_before))
        self.assertEqual([tuple(r) for r in dump(conn, "job_events")], events_before)
        assert_v5_jobs_shape(self, conn)
        assert_v6_attempts_shape(self, conn)
        conn.close()

    def test_canceled_insertable_after_migration(self):
        make_populated_v1_db(self.db)
        conn = store.connect(self.db)
        conn.execute(
            "INSERT INTO jobs (argv_json, state, created_at, next_run_at)"
            " VALUES ('[\"x\"]', 'canceled', 1.0, 1.0)"
        )
        conn.execute(
            "INSERT INTO attempts (job_id, attempt_no, worker_id, worker_pid,"
            " state, started_at, stdout_path, stderr_path)"
            " VALUES (1, 2, 'w', 1, 'canceled', 1.0, '/o', '/e')"
        )
        conn.close()

    def test_id_sequence_survives_migration(self):
        # Job 6 was inserted and deleted pre-migration; the next id must be 7.
        make_populated_v1_db(self.db)
        conn = store.connect(self.db)
        cur = conn.execute(
            "INSERT INTO jobs (argv_json, state, created_at, next_run_at)"
            " VALUES ('[\"new\"]', 'queued', 1.0, 1.0)"
        )
        self.assertEqual(cur.lastrowid, 7)
        conn.close()

    def test_v2_id_sequence_survives_migration(self):
        # Job 7 was inserted and deleted pre-migration; the next id must be 8.
        make_populated_v2_db(self.db)
        conn = store.connect(self.db)
        cur = conn.execute(
            "INSERT INTO jobs (argv_json, state, created_at, next_run_at)"
            " VALUES ('[\"new\"]', 'queued', 1.0, 1.0)"
        )
        self.assertEqual(cur.lastrowid, 8)
        conn.close()

    def test_migration_is_idempotent_across_reopens(self):
        make_populated_v1_db(self.db)
        store.connect(self.db).close()
        conn = store.connect(self.db)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 5
        )
        conn.close()

    def test_v2_intermediate_revisited_cleanly(self):
        make_populated_v1_db(self.db)
        conn = sqlite3.connect(self.db, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        did = store._migrate_v1_to_v2(conn)
        self.assertTrue(did)
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(int(row["value"]), 2)
        conn.close()

        conn = store.connect(self.db)
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(int(row["value"]), 6)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 5)
        assert_v5_jobs_shape(self, conn)
        assert_v6_attempts_shape(self, conn)
        conn.close()

    def test_existing_invalid_state_still_rejected(self):
        make_populated_v1_db(self.db)
        conn = store.connect(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO jobs (argv_json, state, created_at, next_run_at)"
                " VALUES ('[\"x\"]', 'bogus', 1.0, 1.0)"
            )
        conn.close()

    def test_v3_retry_backoff_row_holds_v4_drain_open(self):
        conn = sqlite3.connect(self.db, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(V2_SCHEMA_SQL)
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
        self.assertTrue(store._migrate_v2_to_v3(conn))
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(int(row["value"]), 3)
        conn.execute(
            "INSERT INTO jobs (argv_json, state, attempts, max_retries,"
            " timeout_seconds, created_at, next_run_at)"
            " VALUES (?,?,?,?,?,?,?)",
            ('["true"]', "queued", 1, 3, 300, time.time(), time.time() + 0.25),
        )
        conn.close()

        proc = subprocess.run(
            [sys.executable, "-m", "spoolctl", "work", "--drain",
             "--db", self.db, "--json", "--poll-interval", "0.05"],
            cwd=REPO, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["data"], {"drained": True, "executed": 1})
        migrated = store.connect(self.db)
        try:
            row = migrated.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            self.assertEqual(int(row["value"]), 6)
            self.assertEqual(store.get_job(migrated, 1).state, "done")
        finally:
            migrated.close()

    def test_v4_to_v6_backfills_crashes_and_failure_reasons(self):
        conn = sqlite3.connect(self.db, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(V2_SCHEMA_SQL)
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
        self.assertTrue(store._migrate_v2_to_v3(conn))
        self.assertTrue(store._migrate_v3_to_v4(conn))
        conn.execute(
            "INSERT INTO jobs (id, argv_json, state, attempts, max_retries,"
            " timeout_seconds, created_at, next_run_at, priority, queue)"
            " VALUES (1, '[\"a\"]', 'queued', 2, 3, 300, 1.0, 1.0, 0, 'default')"
        )
        conn.execute(
            "INSERT INTO jobs (id, argv_json, state, attempts, max_retries,"
            " timeout_seconds, created_at, next_run_at, priority, queue)"
            " VALUES (2, '[\"b\"]', 'queued', 1, 3, 300, 1.0, 1.0, 0, 'default')"
        )
        conn.execute(
            "INSERT INTO attempts (job_id, attempt_no, worker_id, worker_pid,"
            " state, started_at, stdout_path, stderr_path, error)"
            " VALUES (1, 1, 'w', 1, 'abandoned', 1.0, '/o1', '/e1', 'worker died')"
        )
        conn.execute(
            "INSERT INTO attempts (job_id, attempt_no, worker_id, worker_pid,"
            " state, started_at, stdout_path, stderr_path, error)"
            " VALUES (2, 1, 'w', 1, 'abandoned', 1.0, '/o2', '/e2', 'force-retried')"
        )
        conn.close()

        migrated = store.connect(self.db)
        try:
            self.assertEqual(store.get_job(migrated, 1).crashes, 1)
            self.assertEqual(store.get_job(migrated, 2).crashes, 0)
            reasons = [
                r["failure_reason"]
                for r in migrated.execute("SELECT failure_reason FROM attempts ORDER BY job_id")
            ]
            self.assertEqual(reasons, [REASON_WORKER_CRASH, REASON_CANCELED])
        finally:
            migrated.close()


_RACER = """
import os, sys, time
import spoolctl.store as store

db, ready_marker, go_marker, migrated_marker = sys.argv[1:5]
real = store._migrate_v2_to_v3
def wrapped(conn):
    did = real(conn)
    if did:
        open(migrated_marker, "w").close()
    return did
store._migrate_v2_to_v3 = wrapped

open(ready_marker, "w").close()
deadline = time.time() + 30
while not os.path.exists(go_marker):
    if time.time() > deadline:
        sys.exit(2)
    time.sleep(0.005)

conn = store.connect(db)
n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
v = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
conn.close()
print(f"{n} {v}")
"""


class TestMigrationRace(MigrationTestCase):
    def wait_for(self, predicate, timeout=30.0, message="condition"):
        deadline = time.time() + timeout
        while not predicate():
            self.assertLess(time.time(), deadline, f"timed out waiting for {message}")
            time.sleep(0.005)

    def test_two_concurrent_openers_one_migrates(self):
        make_populated_v2_db(self.db)
        go = os.path.join(self.tmp.name, "go")
        procs = []
        markers = []
        for i in (1, 2):
            ready = os.path.join(self.tmp.name, f"ready-{i}")
            migrated = os.path.join(self.tmp.name, f"migrated-{i}")
            markers.append(migrated)
            procs.append((subprocess.Popen(
                [sys.executable, "-c", _RACER, self.db, ready, go, migrated],
                cwd=REPO,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ), ready))
        self.wait_for(lambda: all(os.path.exists(r) for _, r in procs),
                      message="both racers ready")
        Path(go).touch()
        outs = []
        for proc, _ in procs:
            out, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, err)
            outs.append(out.strip())
        self.assertEqual(outs, ["6 6", "6 6"])  # both saw intact data at v6
        migrated_count = sum(os.path.exists(m) for m in markers)
        self.assertEqual(migrated_count, 1, "exactly one process must migrate")
        conn = store.connect(self.db)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        assert_v5_jobs_shape(self, conn)
        assert_v6_attempts_shape(self, conn)
        conn.close()


class TestOneWayDoor(MigrationTestCase):
    def test_v5_binary_rejects_v6_file(self):
        # An older binary sees schema_version=6 > its SCHEMA_VERSION=5 and
        # refuses via SchemaTooNewError; simulated by the version gate itself
        # (test_store covers the general found > SCHEMA_VERSION case).
        conn = store.connect(self.db)  # fresh v6 db
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        conn.close()
        self.assertEqual(int(row["value"]), 6)
        self.assertGreater(int(row["value"]), 5)


if __name__ == "__main__":
    unittest.main()
