"""Reaper: confirmed-dead only, every ambiguity resolves to assume-alive."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

from spoolctl import store, worker


def spawn_fake_worker(*, looks_like_spoolctl: bool) -> subprocess.Popen:
    # ps -o command= shows full argv, so an extra argv token is enough to
    # make (or not make) the process look like a spoolctl worker.
    argv = [sys.executable, "-c", "import time; time.sleep(120)"]
    if looks_like_spoolctl:
        argv.append("spoolctl-worker")
    return subprocess.Popen(argv)


class ReaperTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")
        self.out_root = store.output_root(self.db)
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)

    def claim_with_pid(self, pid: int, max_retries=3, max_crashes=None) -> int:
        job_id = store.add_job(
            self.conn, ["true"], 300, max_retries, 100.0,
            max_crashes=max_crashes,
        )
        store.claim_next(self.conn, f"w-{pid}", pid, 100.0, self.out_root)
        return job_id

    def reap_now(self):
        # Heartbeat was written at claim time (100.0); real now is far past
        # 100.0 + REAP_THRESHOLD, so the row is a candidate.
        err = io.StringIO()
        with redirect_stderr(err):
            reaped = worker.reap_pass(self.conn)
        return reaped, err.getvalue()


class TestLiveness(unittest.TestCase):
    def test_gone_pid_is_dead(self):
        proc = spawn_fake_worker(looks_like_spoolctl=False)
        proc.kill()
        proc.wait()
        self.assertTrue(worker.is_worker_pid_dead(proc.pid))

    def test_live_spoolctl_lookalike_is_alive(self):
        proc = spawn_fake_worker(looks_like_spoolctl=True)
        try:
            self.assertFalse(worker.is_worker_pid_dead(proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_live_unrelated_pid_is_recycled_hence_dead(self):
        proc = spawn_fake_worker(looks_like_spoolctl=False)
        try:
            self.assertTrue(worker.is_worker_pid_dead(proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_ps_failure_is_inconclusive_hence_alive(self):
        proc = spawn_fake_worker(looks_like_spoolctl=False)
        try:
            with mock.patch.object(worker.subprocess, "run",
                                   side_effect=OSError("no ps")):
                self.assertFalse(worker.is_worker_pid_dead(proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_ps_nonzero_is_inconclusive_hence_alive(self):
        proc = spawn_fake_worker(looks_like_spoolctl=False)
        try:
            fake = subprocess.CompletedProcess([], returncode=1, stdout="", stderr="")
            with mock.patch.object(worker.subprocess, "run", return_value=fake):
                self.assertFalse(worker.is_worker_pid_dead(proc.pid))
        finally:
            proc.kill()
            proc.wait()

    def test_permission_error_is_alive(self):
        with mock.patch.object(worker.os, "kill", side_effect=PermissionError):
            self.assertFalse(worker.is_worker_pid_dead(12345))


class TestReapPass(ReaperTestCase):
    def test_dead_worker_job_reaped_and_requeued(self):
        proc = spawn_fake_worker(looks_like_spoolctl=True)
        proc.kill()
        proc.wait()
        job_id = self.claim_with_pid(proc.pid)
        reaped, err = self.reap_now()
        self.assertEqual(reaped, [job_id])
        self.assertIn("reaped", err)
        j = store.get_job(self.conn, job_id)
        self.assertEqual(j.state, "queued")
        self.assertEqual(j.attempts, 1)
        self.assertEqual(j.last_error, "worker died")
        att = self.conn.execute(
            "SELECT state, error FROM attempts WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual((att["state"], att["error"]), ("abandoned", "worker died"))
        events = [r["event"] for r in self.conn.execute(
            "SELECT event FROM job_events WHERE job_id=? ORDER BY id", (job_id,))]
        self.assertIn("reaped", events)

    def test_sigstopped_live_worker_not_reaped_then_reaped_after_kill(self):
        proc = spawn_fake_worker(looks_like_spoolctl=True)
        try:
            job_id = self.claim_with_pid(proc.pid)
            os.kill(proc.pid, signal.SIGSTOP)
            reaped, _ = self.reap_now()
            self.assertEqual(reaped, [], "live-but-stopped worker must not be reaped")
            self.assertEqual(store.get_job(self.conn, job_id).state, "running")
            os.kill(proc.pid, signal.SIGCONT)
        finally:
            proc.kill()
            proc.wait()
        reaped, _ = self.reap_now()
        self.assertEqual(reaped, [job_id])
        self.assertEqual(store.get_job(self.conn, job_id).state, "queued")

    def test_fresh_heartbeat_not_a_candidate(self):
        proc = spawn_fake_worker(looks_like_spoolctl=True)
        proc.kill()
        proc.wait()
        job_id = self.claim_with_pid(proc.pid)
        store.update_heartbeat(self.conn, job_id, f"w-{proc.pid}", proc.pid, time.time())
        reaped, _ = self.reap_now()
        self.assertEqual(reaped, [], "fresh heartbeat must not be nominated")

    def test_reap_exhausts_crash_budget_to_dead(self):
        proc = spawn_fake_worker(looks_like_spoolctl=True)
        proc.kill()
        proc.wait()
        job_id = self.claim_with_pid(proc.pid, max_retries=0, max_crashes=0)
        reaped, _ = self.reap_now()
        self.assertEqual(reaped, [job_id])
        job = store.get_job(self.conn, job_id)
        self.assertEqual(job.state, "dead")
        self.assertEqual((job.attempts, job.crashes), (1, 1))

    def test_env_override_shrinks_threshold(self):
        with mock.patch.dict(os.environ, {"SPOOLCTL_TEST_REAP_THRESHOLD": "0.01"}):
            self.assertEqual(worker.reap_threshold(), 0.01)
        self.assertEqual(worker.reap_threshold(), float(max(4 * 5, 30)))


class TestHeartbeatThread(ReaperTestCase):
    def test_heartbeat_advances_while_running(self):
        job_id = store.add_job(self.conn, ["true"], 300, 3, time.time())
        store.claim_next(self.conn, "w1", os.getpid(), time.time(), self.out_root)
        before = store.get_job(self.conn, job_id).heartbeat_at
        with mock.patch.dict(os.environ, {"SPOOLCTL_TEST_HEARTBEAT_INTERVAL": "0.05"}):
            with worker.Heartbeat(self.db, job_id, "w1", os.getpid()):
                time.sleep(0.4)
        after = store.get_job(self.conn, job_id).heartbeat_at
        self.assertGreater(after, before)

    def test_heartbeat_respects_ownership_guard(self):
        job_id = store.add_job(self.conn, ["true"], 300, 3, 100.0)
        store.claim_next(self.conn, "w1", 111, 100.0, self.out_root)
        store.update_heartbeat(self.conn, job_id, "someone-else", 999, time.time())
        self.assertEqual(store.get_job(self.conn, job_id).heartbeat_at, 100.0)


if __name__ == "__main__":
    unittest.main()
