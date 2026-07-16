"""status verb: zero-filled counts, recent_dead ordering, always exit 0."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from spoolctl import cli, store


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class StatusTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def make_dead(self, n: int, base_ts: float = 1000.0) -> list[int]:
        conn = store.connect(self.db)
        ids = []
        for i in range(n):
            job_id = store.add_job(conn, ["false", f"job{i}"], 300, 0, 10.0)
            _, attempt = store.claim_next(conn, "w1", 42, 10.0, store.output_root(self.db))
            store.record_failure(conn, job_id, attempt.id, "w1", 42,
                                 "failed", 1, "exit 1", base_ts + i)
            ids.append(job_id)
        conn.close()
        return ids


class TestCounts(StatusTestCase):
    def test_fresh_directory_no_db_exits_zero_with_zero_counts(self):
        code, out, _ = run_cli("status", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        env = json.loads(out)
        self.assertTrue(env["ok"])
        self.assertEqual(env["data"]["counts"],
                         {"canceled": 0, "dead": 0, "done": 0, "failed": 0,
                          "queued": 0, "running": 0})
        self.assertEqual(env["data"]["scheduled"], 0)
        self.assertEqual(env["data"]["queues"], {})
        self.assertEqual(env["data"]["recent_dead"], [])

    def test_counts_reflect_states(self):
        conn = store.connect(self.db)
        store.add_job(conn, ["true"], 300, 3, 10.0)
        store.add_job(conn, ["true"], 300, 3, 10.0)
        j3 = store.add_job(conn, ["true"], 300, 3, 10.0)
        _, att = store.claim_next(conn, "w1", 42, 10.0, store.output_root(self.db))
        store.record_success(conn, 1, att.id, "w1", 42, 20.0)
        _ = j3
        conn.close()
        code, out, _ = run_cli("status", "--db", self.db, "--json")
        counts = json.loads(out)["data"]["counts"]
        self.assertEqual(counts, {"canceled": 0, "dead": 0, "done": 1, "failed": 0,
                                  "queued": 2, "running": 0})

    def test_scheduled_and_queue_counts(self):
        conn = store.connect(self.db)
        future = time.time() + 1000.0
        store.add_job(conn, ["true"], 300, 3, 10.0, next_run_at=future)
        store.add_job(conn, ["true"], 300, 3, 10.0, queue="gpu", next_run_at=future)
        store.add_job(conn, ["true"], 300, 3, 10.0, queue="gpu")
        conn.close()
        code, out, _ = run_cli("status", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        self.assertEqual(data["counts"]["queued"], 3)
        self.assertEqual(data["scheduled"], 2)
        self.assertEqual(data["queues"]["default"]["counts"]["queued"], 1)
        self.assertEqual(data["queues"]["default"]["scheduled"], 1)
        self.assertEqual(data["queues"]["gpu"]["counts"]["queued"], 2)
        self.assertEqual(data["queues"]["gpu"]["scheduled"], 1)


class TestRecentDead(StatusTestCase):
    def test_limit_and_descending_order(self):
        ids = self.make_dead(5)
        code, out, _ = run_cli("status", "--db", self.db, "--json", "--limit", "3")
        dead = json.loads(out)["data"]["recent_dead"]
        self.assertEqual([d["id"] for d in dead], [ids[4], ids[3], ids[2]])

    def test_entry_shape(self):
        self.make_dead(1)
        code, out, _ = run_cli("status", "--db", self.db, "--json")
        d = json.loads(out)["data"]["recent_dead"][0]
        self.assertEqual(
            set(d),
            {"attempts", "command", "finished_at", "id", "last_error",
             "stderr_path", "stdout_path", "crashes"},
        )
        self.assertEqual(d["command"], "false job0")
        self.assertEqual(d["attempts"], 1)
        self.assertEqual(d["crashes"], 0)
        self.assertIn("stdout", d["stdout_path"])

    def test_long_command_truncated(self):
        conn = store.connect(self.db)
        job_id = store.add_job(conn, ["echo"] + ["x" * 30] * 5, 300, 0, 10.0)
        _, att = store.claim_next(conn, "w1", 42, 10.0, store.output_root(self.db))
        store.record_failure(conn, job_id, att.id, "w1", 42, "failed", 1, "e", 20.0)
        conn.close()
        _, out, _ = run_cli("status", "--db", self.db, "--json")
        cmd = json.loads(out)["data"]["recent_dead"][0]["command"]
        self.assertEqual(len(cmd), 80)
        self.assertTrue(cmd.endswith("..."))

    def test_negative_limit_rejected(self):
        code, _, err = run_cli("status", "--db", self.db, "--limit", "-1")
        self.assertEqual(code, 1)
        self.assertTrue(err.strip())


class TestHumanAndDeterminism(StatusTestCase):
    def test_human_line(self):
        code, out, _ = run_cli("status", "--db", self.db)
        self.assertEqual(code, 0)
        self.assertIn("queued 0", out)
        self.assertIn("dead 0", out)

    def test_human_scheduled_line_and_queue_lines(self):
        conn = store.connect(self.db)
        store.add_job(conn, ["true"], 300, 3, 10.0, next_run_at=time.time() + 1000.0)
        conn.close()
        code, out, _ = run_cli("status", "--db", self.db)
        self.assertEqual(code, 0)
        self.assertIn("scheduled 1", out)
        self.assertNotIn("queues:", out)

        conn = store.connect(self.db)
        store.add_job(conn, ["true"], 300, 3, 10.0, queue="gpu")
        conn.close()
        code, out, _ = run_cli("status", "--db", self.db)
        self.assertEqual(code, 0)
        self.assertIn("queues:", out)
        self.assertIn("  gpu:", out)

    def test_identical_calls_identical_data_hash(self):
        self.make_dead(2)
        _, out1, _ = run_cli("status", "--db", self.db, "--json")
        time.sleep(0.01)
        _, out2, _ = run_cli("status", "--db", self.db, "--json")
        h1 = json.loads(out1)["meta"]["data_hash"]
        h2 = json.loads(out2)["meta"]["data_hash"]
        self.assertEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()
