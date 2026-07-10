"""Worker execution: capture fidelity, timeout, process-group kill, spawn failure."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from spoolctl import store, worker


class ExecTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")
        self.out_root = store.output_root(self.db)
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)

    def claim(self, argv, timeout=300, max_retries=3):
        store.add_job(self.conn, argv, timeout, max_retries, time.time())
        return store.claim_next(self.conn, "w1", os.getpid(), time.time(), self.out_root)


class TestCapture(ExecTestCase):
    def test_stdout_and_stderr_captured_exactly(self):
        job, attempt = self.claim(
            ["sh", "-c", "printf 'out-bytes'; printf 'err-bytes' 1>&2"])
        kind, code, err = worker.execute_attempt(job, attempt)
        self.assertEqual((kind, code, err), ("succeeded", 0, None))
        self.assertEqual(Path(attempt.stdout_path).read_bytes(), b"out-bytes")
        self.assertEqual(Path(attempt.stderr_path).read_bytes(), b"err-bytes")

    def test_binary_output_byte_exact(self):
        job, attempt = self.claim(
            ["sh", "-c", r"printf '\x00\x01\xff\xfe'"])
        worker.execute_attempt(job, attempt)
        self.assertEqual(Path(attempt.stdout_path).read_bytes(), b"\x00\x01\xff\xfe")

    def test_retry_attempts_get_separate_files(self):
        job, a1 = self.claim(["sh", "-c", "printf first; exit 1"])
        kind, code, err = worker.execute_attempt(job, a1)
        self.assertEqual(kind, "failed")
        store.record_failure(self.conn, job.id, a1.id, "w1", os.getpid(),
                             kind, code, err, time.time())
        self.conn.execute("UPDATE jobs SET next_run_at=0, argv_json=?"
                          " WHERE id=?", ('["sh","-c","printf second"]', job.id))
        job2, a2 = store.claim_next(self.conn, "w1", os.getpid(), time.time(), self.out_root)
        worker.execute_attempt(job2, a2)
        self.assertEqual(Path(a1.stdout_path).read_bytes(), b"first")
        self.assertEqual(Path(a2.stdout_path).read_bytes(), b"second")


class TestFailureAndTimeout(ExecTestCase):
    def test_nonzero_exit_is_failed(self):
        job, attempt = self.claim(["sh", "-c", "exit 7"])
        kind, code, err = worker.execute_attempt(job, attempt)
        self.assertEqual((kind, code), ("failed", 7))
        self.assertIn("exit 7", err)

    def test_timeout_kills_within_margin(self):
        job, attempt = self.claim(["sleep", "5"], timeout=1)
        t0 = time.monotonic()
        kind, code, err = worker.execute_attempt(job, attempt)
        elapsed = time.monotonic() - t0
        self.assertEqual(kind, "timed_out")
        self.assertIn("timed out after 1s", err)
        self.assertLess(elapsed, 3.5, "kill must land shortly after the deadline")

    def test_grandchild_dies_with_process_group(self):
        pid_file = os.path.join(self.tmp.name, "grandchild.pid")
        job, attempt = self.claim(
            ["sh", "-c", f"sleep 100 & echo $! > {pid_file}; wait"], timeout=1)
        kind, _, _ = worker.execute_attempt(job, attempt)
        self.assertEqual(kind, "timed_out")
        grandchild = int(Path(pid_file).read_text().strip())
        deadline = time.monotonic() + 3
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                alive = False
                break
        self.assertFalse(alive, f"grandchild {grandchild} survived the group kill")

    def test_spawn_failure_is_normal_failed_attempt(self):
        job, attempt = self.claim(["/nonexistent/binary/xyz"])
        kind, code, err = worker.execute_attempt(job, attempt)
        self.assertEqual(kind, "failed")
        self.assertIsNone(code)
        self.assertIn("spawn failed", err)
        # and the normal retry path accepts it
        state = store.record_failure(self.conn, job.id, attempt.id, "w1",
                                     os.getpid(), kind, code, err, time.time())
        self.assertEqual(state, "queued")


if __name__ == "__main__":
    unittest.main()
