"""feedback operation: the state matrix, the three counting fields, streams,
tail decoding, and byte bounds.

Every row of the state matrix is a contract. `queued` occupies three of them
and the discriminator is the counting fields, not the state string.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from spoolctl import store
from spoolctl.errors import CliError
from spoolctl.models import (
    FAILURE_REASONS,
    FEEDBACK_TAIL_BYTES,
    FEEDBACK_TAIL_MAX,
    REASON_CANCELED,
    REASON_PROCESS_EXIT,
    REASON_SPAWN_FAILED,
    REASON_TIMEOUT,
    REASON_UNKNOWN,
    REASON_WORKER_CRASH,
)
from spoolctl.operations import FeedbackInput, feedback_operation


class FeedbackTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        self.out_root = store.output_root(self.db)

    # --- fixtures -------------------------------------------------------

    def add(self, max_retries: int = 0) -> int:
        return store.add_job(self.conn, ["echo", "hi"], 300, max_retries, 10.0)

    def claim(self, worker: str = "w1", pid: int = 42, now: float = 11.0):
        _, attempt = store.claim_next(self.conn, worker, pid, now, self.out_root)
        for path in (attempt.stdout_path, attempt.stderr_path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            Path(path).write_bytes(b"")
        return attempt

    def write_stream(self, attempt, name: str, body: bytes) -> str:
        path = getattr(attempt, f"{name}_path")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_bytes(body)
        return path

    def make_queued_fresh(self) -> int:
        return self.add()

    def make_queued_backoff(self) -> int:
        job_id = self.add(max_retries=3)
        attempt = self.claim()
        store.record_failure(self.conn, job_id, attempt.id, "w1", 42,
                             "failed", 7, "exit 7", 12.0)
        return job_id

    def make_queued_manual_retry(self) -> int:
        job_id = self.make_dead()
        store.retry_job(self.conn, job_id, False, 10_000.0)
        return job_id

    def make_running(self) -> int:
        job_id = self.add()
        self.claim()
        return job_id

    def make_done(self) -> int:
        job_id = self.add()
        attempt = self.claim()
        store.record_success(self.conn, job_id, attempt.id, "w1", 42, 15.0)
        return job_id

    def make_dead(self) -> int:
        job_id = self.add()
        attempt = self.claim()
        store.record_failure(self.conn, job_id, attempt.id, "w1", 42,
                             "failed", 3, "exit 3", 12.0)
        return job_id

    def make_canceled(self, with_attempt: bool = True) -> int:
        job_id = self.add()
        if with_attempt:
            self.claim()
            store.cancel_job(self.conn, job_id, True, 13.0)
        else:
            store.cancel_job(self.conn, job_id, False, 13.0)
        return job_id

    def make_failed(self) -> int:
        """`failed` has no write path in store.py or worker.py; the row has to
        be fabricated to prove the operation handles it defensively."""
        job_id = self.make_queued_backoff()
        self.conn.execute("UPDATE jobs SET state='failed' WHERE id=?", (job_id,))
        self.conn.commit()
        return job_id

    # --- helpers --------------------------------------------------------

    def feedback(self, job_id: int, tail_bytes: int = FEEDBACK_TAIL_BYTES):
        return feedback_operation(
            FeedbackInput(
                db_path=self.db,
                job_id=job_id,
                tail_bytes=tail_bytes,
                base_dir=self.tmp.name,
            )
        )

    def row(self, job_id: int) -> tuple:
        data = self.feedback(job_id).data
        return (data["state"], data["terminal"], data["succeeded"],
                data["exit_code"], data["failure_reason"], data["remediation"])


class TestStateMatrix(FeedbackTestCase):
    def test_queued_fresh(self):
        job_id = self.make_queued_fresh()
        self.assertEqual(
            self.row(job_id),
            ("queued", False, None, None, None, "spoolctl work --drain"),
        )

    def test_queued_retry_backoff(self):
        job_id = self.make_queued_backoff()
        self.assertEqual(
            self.row(job_id),
            ("queued", False, None, 7, REASON_PROCESS_EXIT, f"spoolctl show {job_id}"),
        )

    def test_queued_manual_retry(self):
        job_id = self.make_queued_manual_retry()
        self.assertEqual(
            self.row(job_id),
            ("queued", False, None, None, None, "spoolctl work --drain"),
        )

    def test_running(self):
        job_id = self.make_running()
        self.assertEqual(
            self.row(job_id),
            ("running", False, None, None, None, f"spoolctl wait {job_id}"),
        )

    def test_failed_is_defensively_non_terminal(self):
        job_id = self.make_failed()
        self.assertEqual(
            self.row(job_id),
            ("failed", False, None, 7, REASON_PROCESS_EXIT, f"spoolctl show {job_id}"),
        )

    def test_done(self):
        job_id = self.make_done()
        self.assertEqual(
            self.row(job_id), ("done", True, True, 0, None, None)
        )

    def test_dead(self):
        job_id = self.make_dead()
        self.assertEqual(
            self.row(job_id),
            ("dead", True, False, 3, REASON_PROCESS_EXIT, f"spoolctl output {job_id}"),
        )

    def test_canceled(self):
        job_id = self.make_canceled()
        self.assertEqual(
            self.row(job_id),
            ("canceled", True, False, None, REASON_CANCELED, None),
        )

    def test_canceled_without_any_attempt_has_no_failure_reason(self):
        job_id = self.make_canceled(with_attempt=False)
        self.assertEqual(
            self.row(job_id), ("canceled", True, False, None, None, None)
        )

    def test_every_row_carries_both_stream_entries(self):
        makers = [
            self.make_queued_fresh, self.make_queued_backoff,
            self.make_queued_manual_retry, self.make_running, self.make_done,
            self.make_dead, self.make_canceled, self.make_failed,
        ]
        for make in makers:
            with self.subTest(make.__name__):
                streams = self.feedback(make()).data["streams"]
                self.assertEqual(sorted(streams), ["stderr", "stdout"])
                for entry in streams.values():
                    self.assertEqual(
                        sorted(entry),
                        ["missing", "path", "size_bytes", "tail", "truncated"],
                    )

    def test_terminal_matches_the_wait_definition(self):
        from spoolctl.models import _WAIT_TERMINAL
        cases = {
            "queued": self.make_queued_fresh, "running": self.make_running,
            "done": self.make_done, "dead": self.make_dead,
            "canceled": self.make_canceled, "failed": self.make_failed,
        }
        for state, make in cases.items():
            with self.subTest(state):
                data = self.feedback(make()).data
                self.assertEqual(data["terminal"], data["state"] in _WAIT_TERMINAL)


class TestCountingFields(FeedbackTestCase):
    def test_manual_retry_keeps_history_and_serves_tails(self):
        job_id = self.make_dead()
        attempt = store.get_attempts(self.conn, job_id)[-1]
        self.write_stream(attempt, "stdout", b"boom\n")
        store.retry_job(self.conn, job_id, False, 10_000.0)

        result = self.feedback(job_id)
        data = result.data
        self.assertEqual(data["state"], "queued")
        self.assertEqual(data["attempts"], 0)
        self.assertGreater(data["attempts_total"], 0)
        self.assertEqual(data["latest_attempt_no"], attempt.attempt_no)
        self.assertEqual([w["code"] for w in result.warnings], [])
        self.assertIsNone(data["exit_code"])
        self.assertIsNone(data["failure_reason"])
        self.assertEqual(data["streams"]["stdout"]["tail"], "boom\n")
        self.assertFalse(data["streams"]["stdout"]["missing"])

    def test_latest_attempt_no_is_not_derivable_from_attempts_total(self):
        job_id = self.make_dead()
        store.retry_job(self.conn, job_id, False, 10_000.0)
        self.claim("w2", 43, 10_001.0)
        data = self.feedback(job_id).data
        self.assertEqual((data["attempts"], data["attempts_total"]), (0, 2))
        self.assertEqual(data["latest_attempt_no"], 2)

    def test_fresh_job_warns_and_reports_zero_counts(self):
        job_id = self.make_queued_fresh()
        result = self.feedback(job_id)
        self.assertEqual([w["code"] for w in result.warnings], ["NO_ATTEMPTS_YET"])
        self.assertEqual(
            (result.data["attempts"], result.data["attempts_total"],
             result.data["latest_attempt_no"]),
            (0, 0, None),
        )


class TestStreams(FeedbackTestCase):
    def test_three_way_distinction_between_empty_deleted_and_never_attempted(self):
        """Empty, captured-then-deleted, and never-attempted must stay three
        distinct payloads; collapsing any pair loses the answer."""
        empty_id = self.make_done()
        empty = self.feedback(empty_id).data["streams"]["stdout"]
        self.assertEqual(
            (empty["missing"], empty["size_bytes"], empty["tail"]), (False, 0, "")
        )
        self.assertIsNotNone(empty["path"])

        deleted_id = self.make_dead()
        attempt = store.get_attempts(self.conn, deleted_id)[-1]
        self.write_stream(attempt, "stdout", b"gone soon\n")
        os.unlink(attempt.stdout_path)
        deleted = self.feedback(deleted_id).data["streams"]["stdout"]
        self.assertTrue(deleted["missing"])
        self.assertEqual(deleted["path"], attempt.stdout_path)
        self.assertEqual((deleted["size_bytes"], deleted["tail"]), (0, ""))

        never_id = self.make_queued_fresh()
        never_result = self.feedback(never_id)
        never = never_result.data["streams"]["stdout"]
        self.assertTrue(never["missing"])
        self.assertIsNone(never["path"])
        self.assertEqual([w["code"] for w in never_result.warnings], ["NO_ATTEMPTS_YET"])

    def test_streams_come_from_the_latest_attempt(self):
        job_id = self.add(max_retries=3)
        first = self.claim()
        self.write_stream(first, "stdout", b"first\n")
        store.record_failure(self.conn, job_id, first.id, "w1", 42,
                             "failed", 1, "exit 1", 12.0)
        second = self.claim("w2", 43, 10_000.0)
        self.write_stream(second, "stdout", b"second\n")
        data = self.feedback(job_id).data
        self.assertEqual(data["latest_attempt_no"], second.attempt_no)
        self.assertEqual(data["streams"]["stdout"]["tail"], "second\n")

    def test_stderr_is_reported_independently_of_stdout(self):
        job_id = self.make_dead()
        attempt = store.get_attempts(self.conn, job_id)[-1]
        self.write_stream(attempt, "stderr", b"trace\n")
        streams = self.feedback(job_id).data["streams"]
        self.assertEqual(streams["stderr"]["tail"], "trace\n")
        self.assertEqual(streams["stdout"]["tail"], "")


class TestTails(FeedbackTestCase):
    def tail_of(self, body: bytes, tail_bytes: int) -> dict:
        job_id = self.make_dead()
        attempt = store.get_attempts(self.conn, job_id)[-1]
        self.write_stream(attempt, "stdout", body)
        return self.feedback(job_id, tail_bytes=tail_bytes).data["streams"]["stdout"]

    def test_tail_is_the_end_not_the_head(self):
        entry = self.tail_of(b"HEADxxxxTAIL", 4)
        self.assertEqual(entry["tail"], "TAIL")
        self.assertTrue(entry["truncated"])
        self.assertEqual(entry["size_bytes"], 12)

    def test_untruncated_stream_is_not_marked_truncated(self):
        entry = self.tail_of(b"short\n", FEEDBACK_TAIL_BYTES)
        self.assertEqual(entry["tail"], "short\n")
        self.assertFalse(entry["truncated"])

    def test_split_multibyte_char_does_not_leak_a_replacement_char(self):
        # The last char is 3 bytes; cutting at 2 orphans its continuation
        # bytes, which are dropped rather than rendered as U+FFFD.
        body = "ab中".encode()
        entry = self.tail_of(body, 2)
        self.assertEqual(entry["tail"], "")
        self.assertFalse(entry["tail"].startswith("�"))
        self.assertTrue(entry["truncated"])

    def test_genuinely_invalid_bytes_survive_as_replacement_chars(self):
        # Same rule, opposite direction: dropping leading bytes "until the
        # decode is clean" would discard the real output after the junk.
        entry = self.tail_of(b"pad" + b"\xff\xfe" + b"real output\n", 13)
        self.assertIn("�", entry["tail"])
        self.assertTrue(entry["tail"].endswith("real output\n"))

    def test_at_most_three_continuation_bytes_are_dropped(self):
        body = b"xx" + "中文".encode()
        entry = self.tail_of(body, 4)
        self.assertEqual(entry["tail"], "文")


class TestBounds(FeedbackTestCase):
    def big_stream(self) -> int:
        job_id = self.make_dead()
        attempt = store.get_attempts(self.conn, job_id)[-1]
        self.write_stream(attempt, "stdout", b"z" * (FEEDBACK_TAIL_BYTES * 2))
        return job_id

    def test_default_tail_bytes_bounds_the_tail(self):
        job_id = self.big_stream()
        entry = feedback_operation(
            FeedbackInput(db_path=self.db, job_id=job_id, base_dir=self.tmp.name)
        ).data["streams"]["stdout"]
        self.assertEqual(len(entry["tail"]), FEEDBACK_TAIL_BYTES)
        self.assertTrue(entry["truncated"])
        self.assertEqual(entry["size_bytes"], FEEDBACK_TAIL_BYTES * 2)

    def test_ceiling_is_accepted(self):
        job_id = self.big_stream()
        entry = self.feedback(job_id, tail_bytes=FEEDBACK_TAIL_MAX)
        self.assertFalse(entry.data["streams"]["stdout"]["truncated"])

    def test_above_ceiling_is_rejected(self):
        job_id = self.big_stream()
        with self.assertRaises(CliError) as ctx:
            self.feedback(job_id, tail_bytes=FEEDBACK_TAIL_MAX + 1)
        self.assertEqual(ctx.exception.code, "INVALID_INPUT")

    def test_zero_and_negative_are_rejected(self):
        job_id = self.big_stream()
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(CliError) as ctx:
                    self.feedback(job_id, tail_bytes=value)
                self.assertEqual(ctx.exception.code, "INVALID_INPUT")


class TestFailureReasonCoverage(FeedbackTestCase):
    def reason_for(self, kind: str, exit_code, reason=None) -> str | None:
        job_id = self.add()
        attempt = self.claim()
        store.record_failure(self.conn, job_id, attempt.id, "w1", 42,
                             kind, exit_code, "err", 12.0, failure_reason=reason)
        return self.feedback(job_id).data["failure_reason"]

    def crash_reason(self) -> str | None:
        """worker_crash is only written by the reaper, never by record_failure."""
        job_id = self.add(max_retries=3)
        self.claim()
        store.reap(self.conn, job_id, 42, 10_000.0, 10_001.0, "reaper")
        return self.feedback(job_id).data["failure_reason"]

    def test_every_failure_reason_value_is_reachable(self):
        seen = {
            self.reason_for("failed", 1),
            self.reason_for("timed_out", None),
            self.reason_for("failed", None, REASON_SPAWN_FAILED),
            self.crash_reason(),
            self.reason_for("failed", None),
            self.feedback(self.make_canceled()).data["failure_reason"],
        }
        self.assertEqual(seen, set(FAILURE_REASONS))
        self.assertEqual(
            seen,
            {REASON_PROCESS_EXIT, REASON_TIMEOUT, REASON_SPAWN_FAILED,
             REASON_WORKER_CRASH, REASON_CANCELED, REASON_UNKNOWN},
        )


class TestFeedbackGrammar(FeedbackTestCase):
    def test_unknown_job_raises_not_found(self):
        with self.assertRaises(CliError) as ctx:
            self.feedback(999)
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_input_requires_keyword_only_base_dir(self):
        with self.assertRaises(TypeError):
            FeedbackInput(self.db, 1, 64)

    def test_duration_is_reported_only_once_the_job_settles(self):
        self.assertIsNone(self.feedback(self.make_running()).data["duration_seconds"])
        done = self.feedback(self.make_done()).data
        self.assertEqual(done["duration_seconds"], 4.0)

    def test_last_error_comes_from_the_job_row(self):
        self.assertEqual(self.feedback(self.make_dead()).data["last_error"], "exit 3")


if __name__ == "__main__":
    unittest.main()
