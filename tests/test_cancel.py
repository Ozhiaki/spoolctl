"""cancel verb: every exit-code rule, row effects, heartbeat-loss proof.

Kill delivery and cancel-vs-completion races run with real worker
subprocesses in test_concurrency.py; this file covers the store/CLI rules
and the Heartbeat thread's proof discipline in-process.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from spoolctl import cli, store, worker


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CancelTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def add_queued(self) -> int:
        conn = store.connect(self.db)
        job_id = store.add_job(conn, ["echo", "hi"], 300, 3, 10.0)
        conn.close()
        return job_id

    def add_running(self) -> int:
        job_id = self.add_queued()
        conn = store.connect(self.db)
        store.claim_next(conn, "w1", 42, 11.0, store.output_root(self.db))
        conn.close()
        return job_id

    def job_row(self, job_id: int) -> dict:
        conn = store.connect(self.db)
        job = store.get_job(conn, job_id)
        conn.close()
        return job.__dict__

    def events(self, job_id: int) -> list[dict]:
        conn = store.connect(self.db)
        evs = store.get_events(conn, job_id)
        conn.close()
        return evs


class TestCancelQueued(CancelTestCase):
    def test_queued_cancels_exit_0(self):
        job_id = self.add_queued()
        code, out, _ = run_cli("cancel", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 0)
        env = json.loads(out)
        self.assertEqual(env["data"], {"job_id": job_id, "state": "canceled",
                                       "was_running": False})
        self.assertEqual(env["warnings"], [])
        row = self.job_row(job_id)
        self.assertEqual(row["state"], "canceled")
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(self.events(job_id)[-1]["event"], "canceled")

    def test_canceled_job_never_claimed(self):
        job_id = self.add_queued()
        run_cli("cancel", str(job_id), "--db", self.db, "--json")
        conn = store.connect(self.db)
        claimed = store.claim_next(conn, "w1", 42, 999.0, store.output_root(self.db))
        conn.close()
        self.assertIsNone(claimed)


class TestCancelRunning(CancelTestCase):
    def test_unforced_is_safety_block_exit_2(self):
        job_id = self.add_running()
        code, out, err = run_cli("cancel", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 2)
        e = json.loads(out)["errors"][0]
        self.assertEqual(e["code"], "SAFETY_BLOCK")
        self.assertIn(f"cancel --running {job_id}", e["remediation"])
        self.assertEqual(self.job_row(job_id)["state"], "running")
        self.assertTrue(err.strip())

    def test_forced_cancels_with_kill_async_warning(self):
        job_id = self.add_running()
        code, out, _ = run_cli("cancel", str(job_id), "--running",
                               "--db", self.db, "--json")
        self.assertEqual(code, 0)
        env = json.loads(out)
        self.assertEqual(env["data"], {"job_id": job_id, "state": "canceled",
                                       "was_running": True})
        self.assertEqual(env["warnings"][0]["code"], "KILL_ASYNC")
        row = self.job_row(job_id)
        self.assertEqual(row["state"], "canceled")
        self.assertIsNone(row["locked_by"])
        self.assertIsNone(row["locked_pid"])
        self.assertIsNone(row["heartbeat_at"])
        conn = store.connect(self.db)
        attempts = store.get_attempts(conn, job_id)
        conn.close()
        self.assertEqual(attempts[-1].state, "canceled")
        self.assertEqual(self.events(job_id)[-1]["detail"], "forced (was running)")

    def test_forced_cancel_discards_late_worker_result(self):
        job_id = self.add_running()
        conn = store.connect(self.db)
        attempt = store.get_attempts(conn, job_id)[-1]
        run_cli("cancel", str(job_id), "--running", "--db", self.db, "--json")
        outcome = store.record_success(conn, job_id, attempt.id, "w1", 42, 99.0)
        conn.close()
        self.assertIsNone(outcome)  # ownership guard rejects the stale result
        self.assertEqual(self.job_row(job_id)["state"], "canceled")


class TestCancelTerminalAndMissing(CancelTestCase):
    def make_state(self, state: str) -> int:
        job_id = self.add_running()
        conn = store.connect(self.db)
        attempt = store.get_attempts(conn, job_id)[-1]
        if state == "done":
            store.record_success(conn, job_id, attempt.id, "w1", 42, 20.0)
        elif state == "dead":
            conn.execute("UPDATE jobs SET max_retries=0 WHERE id=?", (job_id,))
            store.record_failure(conn, job_id, attempt.id, "w1", 42,
                                 "failed", 1, "exit 1", 20.0)
        elif state == "canceled":
            store.cancel_job(conn, job_id, True, 20.0)
        conn.close()
        return job_id

    def test_done_is_conflict_exit_5(self):
        job_id = self.make_state("done")
        code, out, _ = run_cli("cancel", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 5)
        e = json.loads(out)["errors"][0]
        self.assertEqual(e["code"], "CONFLICT")
        self.assertIn("already done", e["message"])

    def test_dead_is_conflict_with_retry_remediation(self):
        job_id = self.make_state("dead")
        code, out, _ = run_cli("cancel", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 5)
        self.assertIn(f"spoolctl retry {job_id}",
                      json.loads(out)["errors"][0]["remediation"])

    def test_canceled_is_conflict_nothing_to_do(self):
        job_id = self.make_state("canceled")
        code, out, _ = run_cli("cancel", str(job_id), "--running",
                               "--db", self.db, "--json")
        self.assertEqual(code, 5)
        self.assertIn("already canceled",
                      json.loads(out)["errors"][0]["remediation"])

    def test_unknown_id_not_found_exit_1(self):
        code, out, _ = run_cli("cancel", "42", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "NOT_FOUND")

    def test_non_integer_id_invalid_input(self):
        code, out, _ = run_cli("cancel", "abc", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")


class TestRetryAfterCancel(CancelTestCase):
    def test_retry_requeues_canceled_with_fresh_budget(self):
        job_id = self.add_running()
        conn = store.connect(self.db)
        conn.execute("UPDATE jobs SET attempts=2 WHERE id=?", (job_id,))
        store.cancel_job(conn, job_id, True, 20.0)
        conn.close()
        code, out, _ = run_cli("retry", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 0)
        row = self.job_row(job_id)
        self.assertEqual((row["state"], row["attempts"]), ("queued", 0))


class TestHeartbeatProofDiscipline(CancelTestCase):
    """on_lost fires on rowcount-0 proof, never on exceptions."""

    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(
            os.environ, {"SPOOLCTL_TEST_HEARTBEAT_INTERVAL": "0.02"})
        self.env.start()
        self.addCleanup(self.env.stop)
        store.connect(self.db).close()  # create schema for the hb connection

    def test_rowcount_zero_fires_on_lost_once(self):
        lost = threading.Event()
        with mock.patch.object(store, "update_heartbeat", return_value=0):
            with worker.Heartbeat(self.db, 1, "w1", 42, on_lost=lost.set):
                self.assertTrue(lost.wait(timeout=10), "on_lost never fired")

    def test_transient_busy_never_fires_on_lost(self):
        beats = threading.Event()
        calls = {"n": 0}

        def busy(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 5:
                beats.set()
            raise sqlite3.OperationalError("database is locked")

        lost = threading.Event()
        with mock.patch.object(store, "update_heartbeat", side_effect=busy):
            with worker.Heartbeat(self.db, 1, "w1", 42, on_lost=lost.set):
                self.assertTrue(beats.wait(timeout=10), "heartbeat stopped beating")
        self.assertFalse(lost.is_set(),
                         "a transient failure must not count as lost ownership")

    def test_owned_row_matches_one(self):
        job_id = self.add_running()
        conn = store.connect(self.db)
        self.assertEqual(store.update_heartbeat(conn, job_id, "w1", 42, 30.0), 1)
        store.cancel_job(conn, job_id, True, 31.0)
        self.assertEqual(store.update_heartbeat(conn, job_id, "w1", 42, 32.0), 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
