"""retry verb: state->exit-code map, fresh budget, --force semantics."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from spoolctl import cli, store


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class RetryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)

    def make_job(self, target_state: str, max_retries=0) -> int:
        job_id = store.add_job(self.conn, ["echo", "a b"], 300, max_retries, 10.0)
        if target_state == "queued":
            return job_id
        _, att = store.claim_next(self.conn, "w1", 4242, 10.0, store.output_root(self.db))
        if target_state == "running":
            return job_id
        if target_state == "done":
            store.record_success(self.conn, job_id, att.id, "w1", 4242, 20.0)
        elif target_state == "dead":
            store.record_failure(self.conn, job_id, att.id, "w1", 4242,
                                 "failed", 1, "exit 1", 20.0)
        return job_id

    def state(self, job_id):
        return store.get_job(self.conn, job_id).state


class TestExitCodeMap(RetryTestCase):
    def test_dead_retries_exit_zero_fresh_budget(self):
        job_id = self.make_job("dead")
        self.assertEqual(self.state(job_id), "dead")
        code, out, _ = run_cli("retry", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 0)
        env = json.loads(out)
        self.assertEqual(env["data"], {"job_id": job_id, "state": "queued"})
        job = store.get_job(self.conn, job_id)
        self.assertEqual((job.state, job.attempts), ("queued", 0))
        self.assertIsNone(job.last_error)
        events = [r["event"] for r in self.conn.execute(
            "SELECT event FROM job_events WHERE job_id=? ORDER BY id", (job_id,))]
        self.assertEqual(events[-1], "retried")

    def test_queued_conflicts_exit_5(self):
        job_id = self.make_job("queued")
        code, out, err = run_cli("retry", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "CONFLICT")
        self.assertIn("already queued", err)

    def test_done_conflicts_exit_5_with_readd_command(self):
        job_id = self.make_job("done")
        code, out, _ = run_cli("retry", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 5)
        e = json.loads(out)["errors"][0]
        self.assertEqual(e["code"], "CONFLICT")
        self.assertIn("spoolctl add -- echo 'a b'", e["remediation"])

    def test_running_safety_blocked_exit_2_names_force(self):
        job_id = self.make_job("running")
        code, out, err = run_cli("retry", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 2)
        e = json.loads(out)["errors"][0]
        self.assertEqual(e["code"], "SAFETY_BLOCK")
        self.assertIn(f"retry --force {job_id}", e["remediation"])
        self.assertIn("confirmed dead", e["remediation"])
        self.assertEqual(self.state(job_id), "running")

    def test_unknown_id_not_found_exit_1(self):
        code, out, _ = run_cli("retry", "999", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "NOT_FOUND")

    def test_non_integer_id_invalid_input(self):
        code, out, _ = run_cli("retry", "abc", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")


class TestForce(RetryTestCase):
    def test_force_requeues_running_and_displaced_recording_is_discarded(self):
        job_id = self.make_job("running")
        att_id = self.conn.execute(
            "SELECT id FROM attempts WHERE job_id=?", (job_id,)).fetchone()["id"]
        code, out, _ = run_cli("retry", "--force", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 0)
        job = store.get_job(self.conn, job_id)
        self.assertEqual((job.state, job.attempts), ("queued", 0))
        att = self.conn.execute(
            "SELECT state, error FROM attempts WHERE id=?", (att_id,)).fetchone()
        self.assertEqual((att["state"], att["error"]), ("abandoned", "force-retried"))
        # The displaced worker (w1, pid 4242) now tries to record success:
        state = store.record_success(self.conn, job_id, att_id, "w1", 4242, 30.0)
        self.assertIsNone(state, "displaced recording must affect zero rows")
        self.assertEqual(self.state(job_id), "queued")

    def test_force_racing_a_state_change_exits_5(self):
        for target in ("queued", "done", "dead"):
            with self.subTest(target=target):
                job_id = self.make_job(target)
                code, out, _ = run_cli("retry", "--force", str(job_id),
                                       "--db", self.db, "--json")
                self.assertEqual(code, 5)
                self.assertEqual(json.loads(out)["errors"][0]["code"], "CONFLICT")

    def test_retry_then_work_reruns_with_fresh_budget(self):
        job_id = self.make_job("dead")
        run_cli("retry", str(job_id), "--db", self.db, "--json")
        code, out, _ = run_cli("work", "--once", "--db", self.db, "--json")
        data = json.loads(out)["data"]
        self.assertEqual((data["job_id"], data["result"]), (job_id, "succeeded"))
        job = store.get_job(self.conn, job_id)
        self.assertEqual(job.state, "done")
        # Budget reset must NOT reset attempt numbering: history keeps
        # attempt 1, the rerun is attempt 2, output paths never collide.
        self.assertEqual(data["attempt_no"], 2)


if __name__ == "__main__":
    unittest.main()
