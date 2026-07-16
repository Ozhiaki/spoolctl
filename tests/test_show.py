"""show verb: full attempt history and event trail, id grammar, not-found."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from spoolctl import cli, store
from spoolctl.models import (
    REASON_PROCESS_EXIT,
    REASON_SPAWN_FAILED,
    REASON_TIMEOUT,
    REASON_UNKNOWN,
)


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ShowTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def make_failed_retried_succeeded(self) -> int:
        """One job through the whole arc: fail (dead), manual retry, succeed."""
        conn = store.connect(self.db)
        out_root = store.output_root(self.db)
        job_id = store.add_job(conn, ["echo", "hi"], 300, 0, 10.0)
        _, a1 = store.claim_next(conn, "w1", 42, 11.0, out_root)
        store.record_failure(conn, job_id, a1.id, "w1", 42, "failed", 1, "exit 1", 12.0)
        store.retry_job(conn, job_id, False, 13.0)
        _, a2 = store.claim_next(conn, "w2", 43, 14.0, out_root)
        store.record_success(conn, job_id, a2.id, "w2", 43, 15.0)
        conn.close()
        return job_id


class TestShowDetail(ShowTestCase):
    def test_full_history_after_fail_retry_succeed(self):
        job_id = self.make_failed_retried_succeeded()
        code, out, _ = run_cli("show", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]

        self.assertEqual(data["job"]["id"], job_id)
        self.assertEqual(data["job"]["state"], "done")
        self.assertIsNone(data["job"]["last_failure_reason"])
        self.assertEqual(data["job"]["argv"], ["echo", "hi"])
        self.assertIsNone(data["job"]["locked_by"])
        self.assertIsNone(data["job"]["idempotency_key"])
        self.assertEqual(data["job"]["priority"], 0)
        self.assertEqual(data["job"]["queue"], "default")
        self.assertEqual(data["job"]["tags"], {})
        self.assertIsNone(data["job"]["note"])

        self.assertEqual(
            [(a["attempt_no"], a["state"], a["exit_code"]) for a in data["attempts"]],
            [(1, "failed", 1), (2, "succeeded", 0)],
        )
        self.assertEqual(data["attempts"][0]["error"], "exit 1")
        self.assertEqual(data["attempts"][0]["failure_reason"], REASON_PROCESS_EXIT)
        self.assertIsNone(data["attempts"][1]["failure_reason"])
        self.assertTrue(data["attempts"][1]["stdout_path"].endswith("/2/stdout"))

        self.assertEqual(
            [e["event"] for e in data["events"]],
            ["added", "claimed", "failed", "dead", "retried", "claimed", "succeeded"],
        )
        self.assertEqual(data["events"][1]["worker_id"], "w1")
        self.assertEqual(data["events"][5]["worker_id"], "w2")

    def test_human_block_has_header_attempts_events(self):
        job_id = self.make_failed_retried_succeeded()
        code, out, _ = run_cli("show", str(job_id), "--db", self.db)
        self.assertEqual(code, 0)
        lines = out.rstrip("\n").split("\n")
        self.assertTrue(lines[0].startswith(f"#{job_id}  done  "))
        self.assertIn("queue: default", lines)
        self.assertIn("priority: 0", lines)
        self.assertIn("next_run_at: 13", lines)
        self.assertIn("  attempt 1  failed  exit=1  worker=w1  error: exit 1", lines)
        self.assertIn("  attempt 2  succeeded  exit=0  worker=w2", lines)
        self.assertIn("events:", lines)

    def test_running_job_shows_lock_columns(self):
        conn = store.connect(self.db)
        store.add_job(conn, ["sleep", "5"], 300, 3, 10.0)
        store.claim_next(conn, "w9", 77, 11.0, store.output_root(self.db))
        conn.close()
        _, out, _ = run_cli("show", "1", "--db", self.db, "--json")
        job = json.loads(out)["data"]["job"]
        self.assertEqual((job["state"], job["locked_by"], job["locked_pid"]),
                         ("running", "w9", 77))
        self.assertIsNone(job["last_failure_reason"])

    def test_dead_nonzero_exit_shows_process_exit_reason(self):
        conn = store.connect(self.db)
        job_id = store.add_job(conn, ["false"], 300, 0, 10.0)
        _, attempt = store.claim_next(conn, "w1", 42, 11.0, store.output_root(self.db))
        store.record_failure(conn, job_id, attempt.id, "w1", 42, "failed", 1, "exit 1", 12.0)
        conn.close()

        _, out, _ = run_cli("show", str(job_id), "--db", self.db, "--json")
        data = json.loads(out)["data"]
        self.assertEqual(data["job"]["last_failure_reason"], REASON_PROCESS_EXIT)
        self.assertEqual(data["attempts"][0]["failure_reason"], REASON_PROCESS_EXIT)

    def test_dead_timeout_and_spawn_failure_show_specific_reasons(self):
        code, out, _ = run_cli(
            "add", "--db", self.db, "--json",
            "--timeout", "1", "--max-retries", "0", "--", "sleep", "5",
        )
        timeout_id = json.loads(out)["data"]["job_id"]
        run_cli("work", "--once", "--db", self.db, "--json")

        code, out, _ = run_cli(
            "add", "--db", self.db, "--json",
            "--max-retries", "0", "--", "/nonexistent/spoolctl-test-binary",
        )
        spawn_id = json.loads(out)["data"]["job_id"]
        run_cli("work", "--once", "--db", self.db, "--json")

        _, out, _ = run_cli("show", str(timeout_id), "--db", self.db, "--json")
        timeout_data = json.loads(out)["data"]
        self.assertEqual(timeout_data["job"]["last_failure_reason"], REASON_TIMEOUT)
        self.assertEqual(timeout_data["attempts"][0]["failure_reason"], REASON_TIMEOUT)

        _, out, _ = run_cli("show", str(spawn_id), "--db", self.db, "--json")
        spawn_data = json.loads(out)["data"]
        self.assertEqual(spawn_data["job"]["last_failure_reason"], REASON_SPAWN_FAILED)
        self.assertEqual(spawn_data["attempts"][0]["failure_reason"], REASON_SPAWN_FAILED)

    def test_unknown_legacy_reason_surfaces_for_current_dead_job(self):
        conn = store.connect(self.db)
        job_id = store.add_job(conn, ["x"], 300, 0, 10.0)
        _, attempt = store.claim_next(conn, "w1", 42, 11.0, store.output_root(self.db))
        store.record_failure(
            conn, job_id, attempt.id, "w1", 42, "failed", None, "legacy", 12.0,
            failure_reason=REASON_UNKNOWN,
        )
        conn.close()

        _, out, _ = run_cli("show", str(job_id), "--db", self.db, "--json")
        data = json.loads(out)["data"]
        self.assertEqual(data["job"]["last_failure_reason"], REASON_UNKNOWN)
        self.assertEqual(data["attempts"][0]["failure_reason"], REASON_UNKNOWN)

    def test_metadata_printed_in_json_and_human_show(self):
        code, out, _ = run_cli(
            "add", "--db", self.db, "--json",
            "--key", "run-1", "--tag", "owner=agent", "--note", "handoff",
            "--priority", "4", "--queue", "gpu",
            "--", "true",
        )
        job_id = json.loads(out)["data"]["job_id"]
        code, out, _ = run_cli("show", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 0)
        job = json.loads(out)["data"]["job"]
        self.assertEqual(job["idempotency_key"], "run-1")
        self.assertEqual((job["priority"], job["queue"]), (4, "gpu"))
        self.assertEqual(job["tags"], {"owner": "agent"})
        self.assertEqual(job["note"], "handoff")

        code, out, _ = run_cli("show", str(job_id), "--db", self.db)
        self.assertEqual(code, 0)
        self.assertIn("key: run-1", out)
        self.assertIn("queue: gpu", out)
        self.assertIn("priority: 4", out)
        self.assertIn("tags: owner=agent", out)
        self.assertIn("note: handoff", out)


class TestShowGrammar(ShowTestCase):
    def test_unknown_id_not_found_exit_1(self):
        code, out, err = run_cli("show", "42", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        e = json.loads(out)["errors"][0]
        self.assertEqual(e["code"], "NOT_FOUND")
        self.assertIn("spoolctl list", e["remediation"])
        self.assertTrue(err.strip())

    def test_non_integer_id_invalid_input(self):
        code, out, _ = run_cli("show", "abc", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")

    def test_missing_id_is_missing_required(self):
        code, out, _ = run_cli("show", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "MISSING_REQUIRED")


if __name__ == "__main__":
    unittest.main()
