"""store.py: schema idempotency, pragmas, path resolution, version gate."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spoolctl import store
from spoolctl.models import SCHEMA_VERSION


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "queue.db")


class TestSchema(StoreTestCase):
    def test_init_is_idempotent(self):
        conn = store.connect(self.db_path)
        conn.close()
        conn = store.connect(self.db_path)
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        conn.close()
        self.assertTrue({"jobs", "attempts", "job_events", "meta"} <= tables)

    def test_schema_version_recorded(self):
        conn = store.connect(self.db_path)
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        conn.close()
        self.assertEqual(int(row["value"]), SCHEMA_VERSION)

    def test_pragmas_set(self):
        conn = store.connect(self.db_path)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)  # NORMAL
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        conn.close()

    def test_parent_dir_auto_created(self):
        nested = os.path.join(self.tmp.name, "a", "b", "queue.db")
        conn = store.connect(nested)
        conn.close()
        self.assertTrue(os.path.exists(nested))

    def test_newer_schema_version_rejected(self):
        conn = store.connect(self.db_path)
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )
        conn.close()
        with self.assertRaises(store.SchemaTooNewError) as ctx:
            store.connect(self.db_path)
        self.assertIn("upgrade spoolctl", str(ctx.exception))


class TestPathResolution(StoreTestCase):
    def test_flag_beats_env_beats_default(self):
        flag = os.path.join(self.tmp.name, "flag.db")
        env = os.path.join(self.tmp.name, "env.db")
        with mock.patch.dict(os.environ, {"SPOOLCTL_DB": env}):
            self.assertEqual(store.resolve_db_path(flag), os.path.realpath(flag))
            self.assertEqual(store.resolve_db_path(None), os.path.realpath(env))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                store.resolve_db_path(None),
                os.path.realpath(store.DEFAULT_DB_RELPATH),
            )

    def test_symlinked_spellings_resolve_to_one_db(self):
        real_dir = Path(self.tmp.name) / "real"
        real_dir.mkdir()
        link_dir = Path(self.tmp.name) / "link"
        link_dir.symlink_to(real_dir)
        a = store.resolve_db_path(str(real_dir / "queue.db"))
        b = store.resolve_db_path(str(link_dir / "queue.db"))
        self.assertEqual(a, b)

    def test_macos_tmp_style_realpath(self):
        # On macOS /var is a symlink to /private/var; the general property is
        # realpath-idempotence for whatever tempdir spelling we were given.
        p = store.resolve_db_path(self.db_path)
        self.assertEqual(p, os.path.realpath(p))


class TestHelpers(StoreTestCase):
    def test_get_job_missing_returns_none(self):
        conn = store.connect(self.db_path)
        self.assertIsNone(store.get_job(conn, 999))
        conn.close()

    def test_add_event_appends(self):
        conn = store.connect(self.db_path)
        conn.execute(
            "INSERT INTO jobs (argv_json, state, created_at, next_run_at) "
            "VALUES ('[\"true\"]', 'queued', 1.0, 1.0)"
        )
        store.add_event(conn, 1, 2.0, "added")
        row = conn.execute("SELECT * FROM job_events").fetchone()
        conn.close()
        self.assertEqual((row["job_id"], row["event"], row["at"]), (1, "added", 2.0))

    def test_output_root_beside_db(self):
        self.assertEqual(
            store.output_root("/x/y/queue.db"), os.path.join("/x/y", "output")
        )


class TestConcurrentOpen(StoreTestCase):
    def test_two_connections_share_schema(self):
        c1 = store.connect(self.db_path)
        c2 = store.connect(self.db_path)
        c1.execute(
            "INSERT INTO jobs (argv_json, state, created_at, next_run_at) "
            "VALUES ('[\"true\"]', 'queued', 1.0, 1.0)"
        )
        row = c2.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
        self.assertEqual(row["n"], 1)
        c1.close()
        c2.close()


if __name__ == "__main__":
    unittest.main()
