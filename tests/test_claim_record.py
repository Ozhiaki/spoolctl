"""Atomic claim and guarded result recording: the mutual-exclusion core."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest

from spoolctl import store


class ClaimTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")
        self.out_root = store.output_root(self.db)
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)

    def add(self, max_retries=3, now=100.0) -> int:
        return store.add_job(self.conn, ["true"], 300, max_retries, now)


class TestClaim(ClaimTestCase):
    def test_claim_returns_job_and_attempt(self):
        job_id = self.add()
        claimed = store.claim_next(self.conn, "w1", 111, 200.0, self.out_root)
        self.assertIsNotNone(claimed)
        job, attempt = claimed
        self.assertEqual(job.id, job_id)
        self.assertEqual(job.state, "running")
        self.assertEqual((job.locked_by, job.locked_pid), ("w1", 111))
        self.assertEqual(attempt.attempt_no, 1)
        self.assertEqual(attempt.state, "running")
        self.assertIn(os.path.join(str(job_id), "1", "stdout"), attempt.stdout_path)

    def test_two_sequential_claims_never_same_job(self):
        self.add()
        self.add()
        c1 = store.claim_next(self.conn, "w1", 111, 200.0, self.out_root)
        c2 = store.claim_next(self.conn, "w2", 222, 200.0, self.out_root)
        self.assertNotEqual(c1[0].id, c2[0].id)
        self.assertIsNone(store.claim_next(self.conn, "w3", 333, 200.0, self.out_root))

    def test_oldest_next_run_at_first(self):
        a = store.add_job(self.conn, ["true"], 300, 3, 100.0)
        b = store.add_job(self.conn, ["true"], 300, 3, 50.0)
        claimed = store.claim_next(self.conn, "w1", 111, 200.0, self.out_root)
        self.assertEqual(claimed[0].id, b)
        _ = a

    def test_future_next_run_at_not_eligible(self):
        self.add(now=100.0)
        self.conn.execute("UPDATE jobs SET next_run_at=999.0")
        self.assertIsNone(store.claim_next(self.conn, "w1", 111, 200.0, self.out_root))

    def test_concurrent_threads_disjoint_claims(self):
        n_jobs, n_workers = 5, 12
        for _ in range(n_jobs):
            self.add()
        results, lock = [], threading.Lock()

        def worker(i):
            conn = store.connect(self.db)
            try:
                got = store.claim_next(conn, f"w{i}", i, 200.0, self.out_root)
                with lock:
                    results.append(got[0].id if got else None)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        claimed = [r for r in results if r is not None]
        self.assertEqual(len(claimed), n_jobs)
        self.assertEqual(len(set(claimed)), n_jobs, "a job was claimed twice")


class TestRecordSuccess(ClaimTestCase):
    def test_success_clears_locks_and_logs_event(self):
        job_id = self.add()
        job, attempt = store.claim_next(self.conn, "w1", 111, 200.0, self.out_root)
        state = store.record_success(self.conn, job_id, attempt.id, "w1", 111, 210.0)
        self.assertEqual(state, "done")
        j = store.get_job(self.conn, job_id)
        self.assertEqual(j.state, "done")
        self.assertIsNone(j.locked_by)
        self.assertIsNone(j.locked_pid)
        self.assertEqual(j.last_exit_code, 0)
        self.assertEqual(j.finished_at, 210.0)
        events = [r["event"] for r in self.conn.execute(
            "SELECT event FROM job_events WHERE job_id=? ORDER BY id", (job_id,))]
        self.assertEqual(events, ["added", "claimed", "succeeded"])

    def test_guarded_success_discarded_after_reassignment(self):
        job_id = self.add()
        job, attempt = store.claim_next(self.conn, "w1", 111, 200.0, self.out_root)
        # Simulate a force-retry reassigning the row out from under w1.
        self.conn.execute(
            "UPDATE jobs SET state='queued', locked_by=NULL, locked_pid=NULL WHERE id=?",
            (job_id,),
        )
        state = store.record_success(self.conn, job_id, attempt.id, "w1", 111, 210.0)
        self.assertIsNone(state)
        j = store.get_job(self.conn, job_id)
        self.assertEqual(j.state, "queued", "stale result must not clobber newer state")

    def test_guard_checks_pid_not_just_worker_id(self):
        job_id = self.add()
        _, attempt = store.claim_next(self.conn, "w1", 111, 200.0, self.out_root)
        state = store.record_success(self.conn, job_id, attempt.id, "w1", 999, 210.0)
        self.assertIsNone(state)


class TestRecordFailure(ClaimTestCase):
    def fail_once(self, job_id, now):
        job, attempt = store.claim_next(self.conn, "w1", 111, now, self.out_root)
        self.assertEqual(job.id, job_id)
        return store.record_failure(
            self.conn, job_id, attempt.id, "w1", 111, "failed", 1, "exit 1", now + 1
        )

    def test_backoff_sequence_2_4_8_then_dead(self):
        job_id = self.add(max_retries=3)
        delays = []
        now = 1000.0
        for i in range(3):
            state = self.fail_once(job_id, now)
            self.assertEqual(state, "queued")
            j = store.get_job(self.conn, job_id)
            delays.append(j.next_run_at - (now + 1))
            self.conn.execute("UPDATE jobs SET next_run_at=? WHERE id=?", (now, job_id))
            now += 100
        self.assertEqual(delays, [2.0, 4.0, 8.0])
        state = self.fail_once(job_id, now)
        self.assertEqual(state, "dead")
        j = store.get_job(self.conn, job_id)
        self.assertEqual(j.attempts, 4, "dead after exactly max_retries+1 executions")
        events = [r["event"] for r in self.conn.execute(
            "SELECT event FROM job_events WHERE job_id=? ORDER BY id", (job_id,))]
        self.assertEqual(events.count("failed"), 4)
        self.assertEqual(events[-1], "dead")

    def test_max_retries_zero_dead_on_first_failure(self):
        job_id = self.add(max_retries=0)
        state = self.fail_once(job_id, 1000.0)
        self.assertEqual(state, "dead")

    def test_timed_out_kind_records_event_and_attempt_state(self):
        job_id = self.add()
        job, attempt = store.claim_next(self.conn, "w1", 111, 100.0, self.out_root)
        state = store.record_failure(
            self.conn, job_id, attempt.id, "w1", 111, "timed_out", None,
            "timeout after 300s", 200.0,
        )
        self.assertEqual(state, "queued")
        row = self.conn.execute(
            "SELECT state FROM attempts WHERE id=?", (attempt.id,)).fetchone()
        self.assertEqual(row["state"], "timed_out")
        events = {r["event"] for r in self.conn.execute(
            "SELECT event FROM job_events WHERE job_id=?", (job_id,))}
        self.assertIn("timed_out", events)

    def test_abandoned_kind_logs_reaped_event(self):
        job_id = self.add()
        job, attempt = store.claim_next(self.conn, "w1", 111, 100.0, self.out_root)
        store.record_failure(
            self.conn, job_id, attempt.id, "w1", 111, "abandoned", None,
            "worker died", 200.0,
        )
        events = {r["event"] for r in self.conn.execute(
            "SELECT event FROM job_events WHERE job_id=?", (job_id,))}
        self.assertIn("reaped", events)
        row = self.conn.execute(
            "SELECT state FROM attempts WHERE id=?", (attempt.id,)).fetchone()
        self.assertEqual(row["state"], "abandoned")

    def test_guarded_failure_discarded_after_reassignment(self):
        job_id = self.add()
        job, attempt = store.claim_next(self.conn, "w1", 111, 100.0, self.out_root)
        self.conn.execute(
            "UPDATE jobs SET locked_by='w2', locked_pid=222 WHERE id=?", (job_id,))
        state = store.record_failure(
            self.conn, job_id, attempt.id, "w1", 111, "failed", 1, "exit 1", 200.0)
        self.assertIsNone(state)
        j = store.get_job(self.conn, job_id)
        self.assertEqual(j.attempts, 0, "stale failure must not bump attempts")

    def test_retry_files_never_clobbered_paths_differ_per_attempt(self):
        job_id = self.add()
        _, a1 = store.claim_next(self.conn, "w1", 111, 100.0, self.out_root)
        store.record_failure(self.conn, job_id, a1.id, "w1", 111, "failed", 1, "x", 101.0)
        self.conn.execute("UPDATE jobs SET next_run_at=0 WHERE id=?", (job_id,))
        _, a2 = store.claim_next(self.conn, "w1", 111, 200.0, self.out_root)
        self.assertNotEqual(a1.stdout_path, a2.stdout_path)
        self.assertEqual(a2.attempt_no, 2)


if __name__ == "__main__":
    unittest.main()
