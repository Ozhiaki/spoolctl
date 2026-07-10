"""Release-gating concurrency and crash-recovery suite.

Real worker subprocesses against temp databases with short SPOOLCTL_TEST_*
thresholds. Every public reliability claim is an assertion here.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from spoolctl import store

REPO = Path(__file__).resolve().parent.parent
FAST_ENV = {
    **os.environ,
    "SPOOLCTL_TEST_HEARTBEAT_INTERVAL": "0.2",
    "SPOOLCTL_TEST_REAP_THRESHOLD": "1.0",
}


class ConcurrencyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")
        self.workers: list[subprocess.Popen] = []
        self.addCleanup(self.stop_all_workers)

    # -- helpers ---------------------------------------------------------

    def cli(self, verb: str, *argv: str) -> str:
        proc = subprocess.run(
            [sys.executable, "-m", "spoolctl", verb, "--db", self.db, "--json", *argv],
            cwd=REPO, env=FAST_ENV, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def add(self, *cmd: str, timeout: int | None = None,
            max_retries: int | None = None) -> int:
        extra: list[str] = []
        if timeout is not None:
            extra += ["--timeout", str(timeout)]
        if max_retries is not None:
            extra += ["--max-retries", str(max_retries)]
        out = self.cli("add", *extra, "--", *cmd)
        return json.loads(out)["data"]["job_id"]

    def spawn_worker(self, worker_id: str | None = None) -> subprocess.Popen:
        argv = [sys.executable, "-m", "spoolctl", "work", "--db", self.db,
                "--poll-interval", "0.05"]
        if worker_id:
            argv += ["--worker-id", worker_id]
        proc = subprocess.Popen(
            argv, cwd=REPO, env=FAST_ENV,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.workers.append(proc)
        return proc

    def stop_all_workers(self):
        for proc in self.workers:
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                    time.sleep(0.2)
                    if proc.poll() is None:
                        proc.send_signal(signal.SIGTERM)  # group-kill in-flight job
                except ProcessLookupError:
                    pass
        for proc in self.workers:
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

    def query(self, sql: str, *params):
        conn = store.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def job(self, job_id: int):
        conn = store.connect(self.db)
        try:
            return store.get_job(conn, job_id)
        finally:
            conn.close()

    def wait_for(self, predicate, timeout: float, message: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for: {message}")

    def wait_for_state(self, job_id: int, state: str, timeout: float):
        self.wait_for(lambda: self.job(job_id).state == state, timeout,
                      f"job {job_id} -> {state} (is {self.job(job_id).state})")


class TestNoDoubleExecution(ConcurrencyTestCase):
    def test_n_jobs_k_workers_each_marker_written_exactly_once(self):
        n_jobs, k_workers = 6, 16
        markers = []
        for i in range(n_jobs):
            marker = os.path.join(self.tmp.name, f"marker-{i}")
            markers.append(marker)
            self.add("sh", "-c", f"echo ran >> {marker}; sleep 0.3")
        for i in range(k_workers):
            self.spawn_worker(f"racer-{i}")
        self.wait_for(
            lambda: self.query("SELECT COUNT(*) AS n FROM jobs WHERE state='done'")[0]["n"] == n_jobs,
            timeout=30, message="all jobs done",
        )
        self.stop_all_workers()
        for marker in markers:
            lines = Path(marker).read_text().splitlines()
            self.assertEqual(len(lines), 1, f"{marker} executed {len(lines)} times")
        n_attempts = self.query("SELECT COUNT(*) AS n FROM attempts")[0]["n"]
        self.assertEqual(n_attempts, n_jobs, "no job may have a second attempt")


class TestSigkillRecovery(ConcurrencyTestCase):
    def test_sigkilled_worker_job_reaped_and_completed(self):
        marker = os.path.join(self.tmp.name, "recovered")
        job_id = self.add("sh", "-c", f"sleep 1.5; echo ok >> {marker}")
        victim = self.spawn_worker("victim")
        self.wait_for_state(job_id, "running", timeout=15)
        victim.kill()  # SIGKILL: no cleanup, no signal handling
        victim.communicate()
        rescuer = self.spawn_worker("rescuer")
        _ = rescuer
        # reap (~1s threshold) + backoff (2s) + rerun (1.5s)
        self.wait_for_state(job_id, "done", timeout=30)
        job = self.job(job_id)
        self.assertEqual(job.attempts, 1, "abandoned attempt must count once")
        # At-least-once, literally: the SIGKILLed worker's child lives in its
        # own session, so it may finish and write the marker before the reaped
        # job re-runs and writes it again. 1 or 2 lines; never 0, never 3+.
        lines = Path(marker).read_text().splitlines()
        self.assertIn(len(lines), (1, 2), lines)
        self.assertEqual(set(lines), {"ok"})
        states = [r["state"] for r in self.query(
            "SELECT state FROM attempts WHERE job_id=? ORDER BY attempt_no", job_id)]
        self.assertEqual(states, ["abandoned", "succeeded"])
        events = [r["event"] for r in self.query(
            "SELECT event FROM job_events WHERE job_id=? ORDER BY id", job_id)]
        self.assertIn("reaped", events)


class TestNoFalsePositiveReap(ConcurrencyTestCase):
    def test_sigstopped_live_worker_never_loses_its_job(self):
        job_id = self.add("sleep", "15")
        victim = self.spawn_worker("stopped-but-alive")
        self.wait_for_state(job_id, "running", timeout=15)
        os.kill(victim.pid, signal.SIGSTOP)
        try:
            watcher = self.spawn_worker("watcher")
            _ = watcher
            # Well past REAP_THRESHOLD=1s: candidate, but pid is alive.
            time.sleep(3.0)
            job = self.job(job_id)
            self.assertEqual(job.state, "running",
                             "live-but-stopped worker's job must not be reclaimed")
            self.assertEqual(job.attempts, 0)
            self.assertEqual(job.locked_by, "stopped-but-alive")
        finally:
            os.kill(victim.pid, signal.SIGCONT)
        victim.kill()
        victim.communicate()
        # Now death is confirmable: reaping proceeds.
        self.wait_for(lambda: self.job(job_id).attempts == 1, timeout=15,
                      message="reap after the worker actually died")


class TestTimeoutCascade(ConcurrencyTestCase):
    def test_always_timing_out_job_reaches_dead_with_backoff_wall_time(self):
        t0 = time.monotonic()
        job_id = self.add("sleep", "30", timeout=1, max_retries=2)
        self.spawn_worker("timeouter")
        self.wait_for_state(job_id, "dead", timeout=40)
        elapsed = time.monotonic() - t0
        # 3 executions x 1s timeout + backoffs 2s + 4s = 9s minimum.
        self.assertGreaterEqual(elapsed, 9.0, "backoff delays must be honored")
        job = self.job(job_id)
        self.assertEqual(job.attempts, 3)
        states = [r["state"] for r in self.query(
            "SELECT state FROM attempts WHERE job_id=? ORDER BY attempt_no", job_id)]
        self.assertEqual(states, ["timed_out"] * 3)


class TestProcessGroupKill(ConcurrencyTestCase):
    def test_grandchild_gone_after_timeout_via_real_worker(self):
        pid_file = os.path.join(self.tmp.name, "grandchild.pid")
        job_id = self.add(
            "sh", "-c", f"sleep 100 & echo $! > {pid_file}; wait",
            timeout=1, max_retries=0)
        self.spawn_worker("groupkiller")
        self.wait_for_state(job_id, "dead", timeout=20)
        grandchild = int(Path(pid_file).read_text().strip())
        def gone():
            try:
                os.kill(grandchild, 0)
                return False
            except ProcessLookupError:
                return True
        self.wait_for(gone, timeout=5, message=f"grandchild {grandchild} to die")


class TestGuardedRecording(ConcurrencyTestCase):
    def test_displaced_workers_result_is_discarded(self):
        marker = os.path.join(self.tmp.name, "job-finished")
        job_id = self.add("sh", "-c", f"sleep 1; touch {marker}")
        victim = self.spawn_worker("displaced")
        self.wait_for_state(job_id, "running", timeout=15)
        # Reassign the row out from under the live worker (what a future
        # `retry --force` does); its recording must then affect zero rows.
        conn = store.connect(self.db)
        conn.execute(
            "UPDATE jobs SET state='queued', locked_by=NULL, locked_pid=NULL,"
            " heartbeat_at=NULL, next_run_at=9999999999 WHERE id=?", (job_id,))
        conn.close()
        # Wait for the child itself to finish, then give the worker a moment
        # to attempt recording; a fixed sleep flakes on slow runners.
        self.wait_for(lambda: os.path.exists(marker), timeout=30,
                      message="job child to finish")
        time.sleep(1.0)
        victim.send_signal(signal.SIGTERM)
        _, err = victim.communicate(timeout=30)
        self.assertIn("discarding stale result", err)
        job = self.job(job_id)
        self.assertEqual(job.state, "queued", "stale success must not clobber the row")
        self.assertEqual(job.attempts, 0)


if __name__ == "__main__":
    unittest.main()
