"""prune verb: duration grammar, state gate, dry-run, files-then-rows order."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from spoolctl import cli, store
from spoolctl.operations import PruneInput, prune_operation
from spoolctl.validation import _parse_duration


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class PruneTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def make_finished(self, state: str, finished_at: float,
                      body: bytes = b"out\n") -> int:
        """A settled job with one attempt whose output files really exist."""
        conn = store.connect(self.db)
        out_root = store.output_root(self.db)
        job_id = store.add_job(conn, ["echo", "x"], 300, 0, finished_at - 10)
        _, attempt = store.claim_next(conn, "w1", 42, finished_at - 5, out_root)
        if state == "done":
            store.record_success(conn, job_id, attempt.id, "w1", 42, finished_at)
        elif state == "dead":
            store.record_failure(conn, job_id, attempt.id, "w1", 42,
                                 "failed", 1, "exit 1", finished_at)
        elif state == "canceled":
            store.cancel_job(conn, job_id, True, finished_at)
        conn.close()
        for path in (attempt.stdout_path, attempt.stderr_path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            Path(path).write_bytes(body)
        return job_id

    def prune(self, *extra: str) -> tuple[int, dict]:
        code, out, _ = run_cli("prune", "--db", self.db, "--json", *extra)
        return code, (json.loads(out) if out else {})

    def job_ids(self) -> list[int]:
        conn = store.connect(self.db)
        ids = [r["id"] for r in conn.execute("SELECT id FROM jobs ORDER BY id")]
        conn.close()
        return ids


class TestDurationGrammar(PruneTestCase):
    def test_suffixes(self):
        self.assertEqual(_parse_duration("90"), 90)
        self.assertEqual(_parse_duration("10s"), 10)
        self.assertEqual(_parse_duration("5m"), 300)
        self.assertEqual(_parse_duration("2h"), 7200)
        self.assertEqual(_parse_duration("1.5h"), 5400)
        self.assertEqual(_parse_duration("1d"), 86400)
        self.assertEqual(_parse_duration("0"), 0)

    def test_garbage_exits_1(self):
        for bad in ("5w", "abc", "-3", "", "d"):
            code, env = self.prune("--older-than", bad)
            self.assertEqual(code, 1, bad)
            self.assertEqual(env["errors"][0]["code"], "INVALID_INPUT", bad)

    def test_missing_older_than_required(self):
        code, out, _ = run_cli("prune", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "MISSING_REQUIRED")


class TestStateGate(PruneTestCase):
    def test_non_terminal_states_rejected(self):
        for bad in ("queued", "running", "failed", "done,queued"):
            code, env = self.prune("--older-than", "0", "--state", bad)
            self.assertEqual(code, 1, bad)
            self.assertEqual(env["errors"][0]["code"], "INVALID_INPUT", bad)

    def test_near_miss_gets_did_you_mean(self):
        code, env = self.prune("--older-than", "0", "--state", "don")
        self.assertEqual(code, 1)
        self.assertEqual(env["errors"][0]["did_you_mean"], "done")

    def test_default_state_is_done_only(self):
        done = self.make_finished("done", 100.0)
        dead = self.make_finished("dead", 100.0)
        code, env = self.prune("--older-than", "0", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(env["data"]["deleted_jobs"], 1)
        self.assertTrue(env["data"]["actual"])
        self.assertTrue(env["data"]["irreversible"])
        self.assertEqual(self.job_ids(), [dead])
        _ = done


class TestPruneBehavior(PruneTestCase):
    def test_without_yes_or_dry_run_is_safety_block_before_db(self):
        db = os.path.join(self.tmp.name, "missing", "queue.db")
        code, out, _ = run_cli("prune", "--older-than", "0", "--db", db, "--json")
        self.assertEqual(code, 2)
        env = json.loads(out)
        self.assertEqual(env["errors"][0]["code"], "SAFETY_BLOCK")
        self.assertIn("--dry-run", env["errors"][0]["remediation"])
        self.assertIn("--yes", env["errors"][0]["remediation"])
        self.assertFalse(os.path.exists(db))

    def test_yes_dry_run_conflict(self):
        code, env = self.prune("--older-than", "0", "--yes", "--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(env["errors"][0]["code"], "INVALID_INPUT")
        self.assertIn("--dry-run", env["errors"][0]["remediation"])
        self.assertIn("--yes", env["errors"][0]["remediation"])

    def test_zero_matches_is_success_with_yes(self):
        code, env = self.prune("--older-than", "0")
        self.assertEqual(code, 2)
        self.assertEqual(env["errors"][0]["code"], "SAFETY_BLOCK")

        code, env = self.prune("--older-than", "0", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(env["data"]["matched"], 0)
        self.assertTrue(env["data"]["actual"])

    def test_age_filter_uses_finished_at(self):
        import time
        old = self.make_finished("done", time.time() - 3600)
        recent = self.make_finished("done", time.time() - 5)
        code, env = self.prune("--older-than", "10m", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(env["data"]["deleted_jobs"], 1)
        self.assertEqual(self.job_ids(), [recent])
        _ = old

    def test_deletes_rows_files_and_reports_bytes(self):
        job_id = self.make_finished("done", 100.0, body=b"x" * 100)
        conn = store.connect(self.db)
        attempt = store.get_attempts(conn, job_id)[0]
        conn.close()
        code, env = self.prune("--older-than", "0", "--yes")
        self.assertEqual(code, 0)
        data = env["data"]
        self.assertEqual(data["deleted_jobs"], 1)
        self.assertEqual(data["deleted_attempts"], 1)
        self.assertGreaterEqual(data["deleted_events"], 1)
        self.assertEqual(data["freed_bytes"], 200)
        self.assertEqual(self.job_ids(), [])
        self.assertFalse(os.path.exists(attempt.stdout_path))
        self.assertFalse(os.path.exists(os.path.dirname(attempt.stdout_path)))

    def test_dry_run_deletes_nothing_reports_counts(self):
        job_id = self.make_finished("dead", 100.0, body=b"y" * 10)
        conn = store.connect(self.db)
        attempt = store.get_attempts(conn, job_id)[0]
        conn.close()
        code, env = self.prune("--older-than", "0", "--state", "dead", "--dry-run")
        self.assertEqual(code, 0)
        data = env["data"]
        self.assertEqual(data, {"actual": False, "deleted_attempts": 1,
                                "deleted_events": 4, "deleted_jobs": 1,
                                "dry_run": True, "freed_bytes": 20,
                                "matched": 1})
        self.assertEqual(self.job_ids(), [job_id])
        self.assertTrue(os.path.exists(attempt.stdout_path))

    def test_rerun_after_partial_failure_finishes_the_job(self):
        # Simulated crash between step 2 (files deleted) and step 3 (rows).
        job_id = self.make_finished("done", 100.0)
        conn = store.connect(self.db)
        attempt = store.get_attempts(conn, job_id)[0]
        conn.close()
        os.unlink(attempt.stdout_path)
        os.unlink(attempt.stderr_path)
        code, env = self.prune("--older-than", "0", "--yes")
        self.assertEqual(code, 0)
        data = env["data"]
        self.assertEqual((data["deleted_jobs"], data["freed_bytes"]), (1, 0))
        self.assertEqual(self.job_ids(), [])

    def test_retried_matched_job_is_skipped_by_the_recheck(self):
        # The re-check inside prune_delete skips a job that stopped matching.
        job_id = self.make_finished("dead", 100.0)
        conn = store.connect(self.db)
        matches = store.prune_matches(conn, ["dead"], 1e12)
        self.assertEqual(matches[0]["job_id"], job_id)
        store.retry_job(conn, job_id, False, 200.0)  # now queued again
        jobs, attempts, events = store.prune_delete(
            conn, [m["job_id"] for m in matches], ["dead"], 1e12)
        conn.close()
        self.assertEqual((jobs, attempts, events), (0, 0, 0))
        self.assertEqual(self.job_ids(), [job_id])


class TestPruneOperation(PruneTestCase):
    """Direct operation entry point: selection, deletion, and counts."""

    def run_op(self, *, dry_run: bool, states=("done", "dead", "canceled"),
               cutoff: float = 10_000.0):
        return prune_operation(
            PruneInput(
                db_path=self.db,
                states=list(states),
                cutoff=cutoff,
                dry_run=dry_run,
                base_dir=self.tmp.name,
            )
        )

    def attempt_paths(self, job_id: int) -> list[str]:
        conn = store.connect(self.db)
        attempts = store.get_attempts(conn, job_id)
        conn.close()
        return [p for a in attempts for p in (a.stdout_path, a.stderr_path)]

    def test_dry_run_counts_without_deleting_rows_or_files(self):
        job_id = self.make_finished("done", 100.0)
        paths = self.attempt_paths(job_id)
        data = self.run_op(dry_run=True)
        self.assertEqual(
            (data["matched"], data["deleted_jobs"], data["actual"], data["dry_run"]),
            (1, 1, False, True),
        )
        self.assertEqual(data["freed_bytes"], sum(os.stat(p).st_size for p in paths))
        self.assertNotIn("irreversible", data)
        self.assertEqual(self.job_ids(), [job_id])
        self.assertTrue(all(os.path.exists(p) for p in paths))

    def test_real_prune_deletes_rows_and_output_files(self):
        job_id = self.make_finished("done", 100.0)
        paths = self.attempt_paths(job_id)
        expected_bytes = sum(os.stat(p).st_size for p in paths)
        data = self.run_op(dry_run=False)
        self.assertEqual(
            (data["matched"], data["deleted_jobs"], data["actual"],
             data["dry_run"], data["irreversible"]),
            (1, 1, True, False, True),
        )
        self.assertEqual(data["freed_bytes"], expected_bytes)
        self.assertEqual(data["deleted_attempts"], 1)
        self.assertGreater(data["deleted_events"], 0)
        self.assertEqual(self.job_ids(), [])
        self.assertFalse(any(os.path.exists(p) for p in paths))

    def test_state_filter_and_cutoff_narrow_the_selection(self):
        old_done = self.make_finished("done", 100.0)
        self.make_finished("dead", 100.0)
        recent = self.make_finished("done", 50_000.0)
        data = self.run_op(dry_run=False, states=("done",))
        self.assertEqual(data["deleted_jobs"], 1)
        self.assertEqual(self.job_ids(), sorted(self.job_ids()))
        self.assertNotIn(old_done, self.job_ids())
        self.assertIn(recent, self.job_ids())

    def test_no_matches_reports_zeroes(self):
        store.connect(self.db).close()
        data = self.run_op(dry_run=False)
        self.assertEqual(
            (data["matched"], data["deleted_jobs"], data["deleted_attempts"],
             data["deleted_events"], data["freed_bytes"]),
            (0, 0, 0, 0, 0),
        )

    def test_missing_output_files_do_not_trip_the_delete(self):
        job_id = self.make_finished("done", 100.0)
        for path in self.attempt_paths(job_id):
            os.unlink(path)
        data = self.run_op(dry_run=False)
        self.assertEqual((data["deleted_jobs"], data["freed_bytes"]), (1, 0))
        self.assertEqual(self.job_ids(), [])

    def test_input_requires_keyword_only_base_dir(self):
        with self.assertRaises(TypeError):
            PruneInput(self.db, ["done"], 10_000.0, True)


if __name__ == "__main__":
    unittest.main()
