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
        pid_file = os.path.join(self.tmp.name, "child.pid")
        marker = os.path.join(self.tmp.name, "job-finished")
        job_id = self.add("sh", "-c", f"echo $$ > {pid_file}; sleep 1; touch {marker}")
        victim = self.spawn_worker("displaced")
        self.wait_for_state(job_id, "running", timeout=15)
        self.wait_for(
            lambda: os.path.exists(pid_file) and Path(pid_file).read_text().strip(),
            timeout=15, message="job child to start")
        child_pid = int(Path(pid_file).read_text())
        # Reassign the row out from under the live worker (what `retry
        # --force` does); its recording must then affect zero rows.
        conn = store.connect(self.db)
        conn.execute(
            "UPDATE jobs SET state='queued', locked_by=NULL, locked_pid=NULL,"
            " heartbeat_at=NULL, next_run_at=9999999999 WHERE id=?", (job_id,))
        conn.close()
        # The attempt ends one of two legal ways: normally the owner's next
        # heartbeat proves lost ownership and kills the child group (v0.2
        # containment); on a slow runner the child may finish first. Either
        # way the worker's recording must match zero rows.
        def attempt_over():
            if os.path.exists(marker):
                return True
            try:
                os.killpg(child_pid, 0)
                return False
            except ProcessLookupError:
                return True
        self.wait_for(attempt_over, timeout=30, message="attempt to end")
        time.sleep(1.0)  # give the worker a moment to attempt recording
        victim.send_signal(signal.SIGTERM)
        _, err = victim.communicate(timeout=30)
        self.assertIn("discarding stale result", err)
        job = self.job(job_id)
        self.assertEqual(job.state, "queued", "stale result must not clobber the row")
        self.assertEqual(job.attempts, 0)


class TestCancelKillDelivery(ConcurrencyTestCase):
    def group_dead(self, pgid: int):
        def check():
            try:
                os.killpg(pgid, 0)
                return False
            except ProcessLookupError:
                return True
        return check

    def child_pid_of(self, pid_file: str) -> int:
        self.wait_for(
            lambda: os.path.exists(pid_file) and Path(pid_file).read_text().strip(),
            timeout=15, message="job child to start")
        return int(Path(pid_file).read_text())

    def test_forced_cancel_kills_child_group_within_a_heartbeat(self):
        pid_file = os.path.join(self.tmp.name, "child.pid")
        job_id = self.add("sh", "-c", f"echo $$ > {pid_file}; sleep 60")
        self.spawn_worker("owner")
        child_pid = self.child_pid_of(pid_file)

        out = self.cli("cancel", str(job_id), "--running")
        env = json.loads(out)
        self.assertTrue(env["data"]["was_running"])
        self.assertEqual(env["warnings"][0]["code"], "KILL_ASYNC")

        # start_new_session makes the sh the group leader; the owning worker
        # notices lost ownership on its next 0.2s heartbeat and group-kills.
        self.wait_for(self.group_dead(child_pid), timeout=15,
                      message="canceled job's process group to die")
        self.assertEqual(self.job(job_id).state, "canceled")

        # The worker survives the discarded result and keeps working.
        job2 = self.add("sh", "-c", "true")
        self.wait_for_state(job2, "done", timeout=15)

    def test_force_retry_also_contains_the_old_child(self):
        # Side benefit of heartbeat-guard delivery: a job force-retried out
        # from under a live worker gets its old child killed, not orphaned.
        pid_file = os.path.join(self.tmp.name, "child.pid")
        job_id = self.add("sh", "-c", f"echo $$ >> {pid_file}; sleep 60")
        self.spawn_worker("owner")
        child_pid = self.child_pid_of(pid_file)

        self.cli("retry", str(job_id), "--force")
        self.wait_for(self.group_dead(child_pid), timeout=15,
                      message="force-retried job's old process group to die")


class TestWaitSurvivesWorkerCrash(ConcurrencyTestCase):
    def test_wait_returns_zero_after_reap_and_rerun(self):
        marker = os.path.join(self.tmp.name, "m")
        job_id = self.add("sh", "-c", f"sleep 1.5; echo ok >> {marker}")
        victim = self.spawn_worker("victim")
        self.wait_for_state(job_id, "running", timeout=15)
        waiter = subprocess.Popen(
            [sys.executable, "-m", "spoolctl", "wait", str(job_id),
             "--db", self.db, "--json", "--poll-interval", "0.1"],
            cwd=REPO, env=FAST_ENV,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        victim.kill()  # SIGKILL mid-job: recovery is reap + rerun
        victim.communicate()
        self.spawn_worker("rescuer")
        out, err = waiter.communicate(timeout=60)
        self.assertEqual(waiter.returncode, 0, err)
        data = json.loads(out)["data"]
        self.assertTrue(data["all_succeeded"])
        self.assertEqual(data["jobs"][str(job_id)]["state"], "done")
        # At-least-once: the SIGKILLed worker's child may finish too.
        lines = Path(marker).read_text().splitlines()
        self.assertIn(len(lines), (1, 2), lines)


class TestDrainSettles(ConcurrencyTestCase):
    def test_drain_waits_out_backoff_and_other_workers_inflight_job(self):
        # Worker H holds a long job; the drainer must not exit while it runs,
        # nor while the flaky job sits in its 2s backoff requeue.
        hold_id = self.add("sleep", "2.5")
        holder = self.spawn_worker("holder")
        self.wait_for_state(hold_id, "running", timeout=15)

        flaky = os.path.join(self.tmp.name, "flaky-marker")
        flaky_id = self.add(
            "sh", "-c",
            f"if [ -f {flaky} ]; then exit 0; else touch {flaky}; exit 1; fi")
        ok_id = self.add("sh", "-c", "true")

        drainer = subprocess.Popen(
            [sys.executable, "-m", "spoolctl", "work", "--drain",
             "--db", self.db, "--json", "--poll-interval", "0.05"],
            cwd=REPO, env=FAST_ENV,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        out, err = drainer.communicate(timeout=60)
        self.assertEqual(drainer.returncode, 0, err)
        data = json.loads(out)["data"]
        self.assertTrue(data["drained"])
        # The drainer ran the flaky job at least twice minus what the holder
        # stole after finishing its hold; between the two workers everything
        # settled and the drainer did real work.
        self.assertGreaterEqual(data["executed"], 1)
        for job_id in (hold_id, flaky_id, ok_id):
            self.assertEqual(self.job(job_id).state, "done", f"job {job_id}")
        conn = store.connect(self.db)
        self.assertEqual(store.unsettled_count(conn), 0)
        conn.close()
        _ = holder  # stopped by stop_all_workers


class TestPruneSafety(ConcurrencyTestCase):
    def test_prune_never_touches_queued_running_or_live_output(self):
        # One worker, one long job in front: while it runs, 1 running + 4
        # queued rows exist and nothing is terminal, so the widest possible
        # prune (--older-than 0, every terminal state) must delete nothing.
        hold_id = self.add("sh", "-c", "echo working; sleep 4")
        quick_ids = [self.add("sh", "-c", "true") for _ in range(4)]
        all_ids = {hold_id, *quick_ids}
        self.spawn_worker("solo")
        self.wait_for_state(hold_id, "running", timeout=15)
        live_path = self.query(
            "SELECT stdout_path FROM attempts WHERE job_id=?", hold_id
        )[0]["stdout_path"]

        # Assert only on prunes that verifiably ran while the job was live:
        # if the row is still running AFTER the prune finished, it was
        # running for the prune's whole duration, so nothing was terminal.
        live_prunes = 0
        while True:
            out = self.cli("prune", "--older-than", "0",
                           "--state", "done,dead,canceled")
            job = self.job(hold_id)
            if job is None or job.state != "running":
                break  # settled (or racing settle); the unit suite covers it
            self.assertEqual(json.loads(out)["data"]["deleted_jobs"], 0)
            survivors = {r["id"] for r in self.query("SELECT id FROM jobs")}
            self.assertEqual(survivors, all_ids,
                             "prune touched a queued or running row")
            self.assertTrue(os.path.exists(live_path),
                            "live attempt output deleted")
            live_prunes += 1
        self.assertGreaterEqual(live_prunes, 1,
                                "no prune completed while the job was live")

        # After everything settles (some rows may already be pruned), the
        # same prune reclaims whatever is left.
        self.wait_for(
            lambda: self.query(
                "SELECT COUNT(*) AS n FROM jobs WHERE state IN"
                " ('queued','running')")[0]["n"] == 0,
            timeout=30, message="queue to settle")
        self.cli("prune", "--older-than", "0", "--state", "done,dead,canceled")
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"], 0)


class TestCancelCompletionRace(ConcurrencyTestCase):
    def test_final_state_exactly_done_or_canceled_and_stable(self):
        n = 12
        ids = [self.add("sh", "-c", "sleep 0.2") for _ in range(n)]
        for i in range(3):
            self.spawn_worker(f"racer-{i}")

        # Hammer cancels while the jobs run and complete. Any exit code is
        # legal here (0 ok, 2 unforced, 5 conflict); the invariant under test
        # is the final state set, not per-call outcomes.
        deadline = time.monotonic() + 60
        def all_terminal():
            row = self.query(
                "SELECT COUNT(*) AS n FROM jobs WHERE state IN"
                " ('done','canceled','dead','failed')")[0]
            return row["n"] == n
        while not all_terminal():
            self.assertLess(time.monotonic(), deadline, "jobs never settled")
            for job_id in ids:
                subprocess.run(
                    [sys.executable, "-m", "spoolctl", "cancel", str(job_id),
                     "--running", "--db", self.db, "--json"],
                    cwd=REPO, env=FAST_ENV, capture_output=True)

        snapshot1 = {r["id"]: r["state"]
                     for r in self.query("SELECT id, state FROM jobs")}
        for job_id, state in snapshot1.items():
            self.assertIn(state, ("done", "canceled"),
                          f"job {job_id} settled as {state}")
        self.stop_all_workers()
        snapshot2 = {r["id"]: r["state"]
                     for r in self.query("SELECT id, state FROM jobs")}
        self.assertEqual(snapshot1, snapshot2,
                         "a terminal state changed after settling")


class TestIdempotencyKeyConcurrency(ConcurrencyTestCase):
    def test_same_key_stampede_returns_one_active_job(self):
        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "spoolctl", "add", "--db", self.db,
                 "--json", "--key", "stampede", "--", "true"],
                cwd=REPO, env=FAST_ENV,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for _ in range(16)
        ]
        payloads = []
        for proc in procs:
            out, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, err)
            payloads.append(json.loads(out)["data"])

        self.assertEqual({p["job_id"] for p in payloads}, {1})
        self.assertEqual({p["state"] for p in payloads}, {"queued"})
        self.assertEqual(sum(1 for p in payloads if not p["deduplicated"]), 1)
        rows = self.query(
            "SELECT id FROM jobs WHERE idempotency_key=?"
            " AND state IN ('queued','running')",
            "stampede",
        )
        self.assertEqual([r["id"] for r in rows], [1])


if __name__ == "__main__":
    unittest.main()
