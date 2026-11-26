"""Lane holds: a held lane starts no new job, its running jobs finish (a drain)."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from spoolctl import cli, store, worker


def run_cli(*argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = cli.main(list(argv))
    return code, json.loads(out.getvalue())


class HoldTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")
        self.out_root = store.output_root(self.db)
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)

    def add(self, lane="default", argv=("true",)):
        store.add_job(self.conn, list(argv), 300, 0, time.time(), queue=lane)

    def claim(self, lane="default"):
        return store.claim_next(self.conn, "w1", os.getpid(), time.time(), self.out_root, lane=lane)


class TestHoldStore(HoldTestCase):
    def test_held_lane_claims_nothing_until_released(self):
        self.add("hv1")
        store.hold_lane(self.conn, "hv1", time.time())
        self.assertIsNone(self.claim("hv1"))
        self.assertTrue(store.release_lane(self.conn, "hv1"))
        self.assertIsNotNone(self.claim("hv1"))

    def test_running_job_finishes_while_its_lane_is_held(self):
        self.add("hv1")
        self.add("hv1")
        job, attempt = self.claim("hv1")
        store.hold_lane(self.conn, "hv1", time.time())
        kind, code, err, reason = worker.execute_attempt(job, attempt)
        state = store.record_success(self.conn, job.id, attempt.id, "w1", os.getpid(), time.time())
        self.assertEqual((kind, state), ("succeeded", "done"))
        self.assertIsNone(self.claim("hv1"), "the held lane started its second job")

    def test_other_lanes_keep_running(self):
        self.add("hv1")
        self.add("mac")
        store.hold_lane(self.conn, "hv1", time.time())
        self.assertIsNone(self.claim("hv1"))
        self.assertIsNotNone(self.claim("mac"))

    def test_hold_is_idempotent_and_keeps_its_first_time(self):
        first, already = store.hold_lane(self.conn, "hv1", 100.0)
        self.assertEqual((first, already), (100.0, False))
        again, already = store.hold_lane(self.conn, "hv1", 200.0)
        self.assertEqual((again, already), (100.0, True))
        self.assertTrue(store.release_lane(self.conn, "hv1"))
        self.assertFalse(store.release_lane(self.conn, "hv1"))

    def test_drain_worker_exits_on_a_held_lane(self):
        # Queued jobs of a held lane don't keep work --drain alive: nothing
        # claims them until release.
        self.add("hv1")
        store.hold_lane(self.conn, "hv1", time.time())
        self.assertEqual(store.unsettled_count(self.conn, lane="hv1"), 0)
        outcome = worker.work_loop(self.db, "w1", 0.01, drain=True, lane="hv1")
        self.assertEqual(outcome, {"drained": True, "executed": 0})

    def test_v6_database_migrates_to_lane_holds(self):
        self.conn.execute("DROP TABLE lane_holds")
        self.conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
        self.conn.close()
        conn = store.connect(self.db)
        self.addCleanup(conn.close)
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        self.assertEqual(version, "7")
        store.hold_lane(conn, "hv1", time.time())
        self.assertEqual([h["queue"] for h in store.lane_holds(conn)], ["hv1"])


class TestHoldCli(HoldTestCase):
    def test_hold_reports_the_lane_and_release_undoes_it(self):
        self.add("hv1")
        self.add("hv1")
        self.claim("hv1")
        code, env = run_cli("hold", "--db", self.db, "--queue", "hv1", "--json")
        self.assertEqual(code, 0)
        data = env["data"]
        self.assertEqual((data["queue"], data["held"], data["changed"]), ("hv1", True, True))
        self.assertEqual((data["running"], data["queued"]), (1, 1))
        self.assertIsInstance(data["held_at"], float)

        code, env = run_cli("status", "--db", self.db, "--json")
        self.assertEqual([h["queue"] for h in env["data"]["holds"]], ["hv1"])

        code, env = run_cli("hold", "--db", self.db, "--queue", "hv1", "--json")
        self.assertFalse(env["data"]["changed"])

        code, env = run_cli("hold", "--db", self.db, "--queue", "hv1", "--release", "--json")
        data = env["data"]
        self.assertEqual((code, data["held"], data["changed"], data["held_at"]), (0, False, True, None))
        code, env = run_cli("status", "--db", self.db, "--json")
        self.assertEqual(env["data"]["holds"], [])

    def test_hold_survives_reopening_the_database(self):
        run_cli("hold", "--db", self.db, "--queue", "hv1", "--json")
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT queue FROM lane_holds").fetchall(), [("hv1",)])


if __name__ == "__main__":
    unittest.main()
