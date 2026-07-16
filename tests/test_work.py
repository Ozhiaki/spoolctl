"""work verb: --once end to end, loop mode stdout silence, signal handling."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from spoolctl import cli, store

REPO = Path(__file__).resolve().parent.parent
FAST_ENV = {
    **os.environ,
    "SPOOLCTL_TEST_HEARTBEAT_INTERVAL": "0.2",
    "SPOOLCTL_TEST_REAP_THRESHOLD": "1.0",
}


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def spawn_worker(db: str, *extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "spoolctl", "work", "--db", db,
         "--poll-interval", "0.1", *extra],
        cwd=REPO, env=FAST_ENV,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


class WorkTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def add(self, *cmd: str, extra: tuple[str, ...] = ()) -> int:
        code, out, _ = run_cli("add", "--db", self.db, "--json", *extra, "--", *cmd)
        assert code == 0, out
        return json.loads(out)["data"]["job_id"]

    def job_state(self, job_id: int) -> str:
        conn = store.connect(self.db)
        state = store.get_job(conn, job_id).state
        conn.close()
        return state


class TestOnce(WorkTestCase):
    def test_empty_queue_exits_zero_claimed_false(self):
        code, out, _ = run_cli("work", "--db", self.db, "--once", "--json")
        self.assertEqual(code, 0)
        env = json.loads(out)
        self.assertTrue(env["ok"])
        self.assertEqual(env["data"], {"claimed": False})

    def test_runs_exactly_one_job_end_to_end(self):
        marker = os.path.join(self.tmp.name, "ran.txt")
        j1 = self.add("sh", "-c", f"echo one > {marker}")
        j2 = self.add("sh", "-c", "true")
        code, out, err = run_cli("work", "--db", self.db, "--once", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        self.assertEqual(
            {k: data[k] for k in ("claimed", "job_id", "result", "job_state", "attempt_no")},
            {"claimed": True, "job_id": j1, "result": "succeeded",
             "job_state": "done", "attempt_no": 1},
        )
        self.assertEqual(Path(marker).read_text().strip(), "one")
        self.assertEqual(self.job_state(j1), "done")
        self.assertEqual(self.job_state(j2), "queued", "--once must not run a second job")

    def test_failed_job_requeued_with_backoff(self):
        j = self.add("sh", "-c", "exit 3")
        code, out, _ = run_cli("work", "--db", self.db, "--once", "--json")
        data = json.loads(out)["data"]
        self.assertEqual((data["result"], data["job_state"]), ("failed", "queued"))
        conn = store.connect(self.db)
        job = store.get_job(conn, j)
        conn.close()
        self.assertEqual(job.attempts, 1)
        self.assertAlmostEqual(job.next_run_at - job.finished_at, 2.0, delta=0.1)

    def test_human_mode_summary(self):
        self.add("true")
        code, out, _ = run_cli("work", "--db", self.db, "--once")
        self.assertEqual(code, 0)
        self.assertIn("succeeded", out)

    def test_worker_id_flag_recorded(self):
        j = self.add("true")
        run_cli("work", "--db", self.db, "--once", "--worker-id", "custom-w")
        conn = store.connect(self.db)
        row = conn.execute("SELECT worker_id FROM attempts WHERE job_id=?", (j,)).fetchone()
        conn.close()
        self.assertEqual(row["worker_id"], "custom-w")

    def test_bad_poll_interval_rejected(self):
        code, _, err = run_cli("work", "--db", self.db, "--poll-interval", "0")
        self.assertEqual(code, 1)
        self.assertIn("poll-interval", err)

    def test_queue_flag_claims_only_that_lane(self):
        default_job = self.add("true")
        gpu_job = self.add("true", extra=("--queue", "gpu"))
        code, out, _ = run_cli("work", "--db", self.db, "--once", "--json",
                               "--queue", "gpu")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["job_id"], gpu_job)
        self.assertEqual(self.job_state(default_job), "queued")

    def test_default_worker_ignores_non_default_lane(self):
        self.add("true", extra=("--queue", "gpu"))
        code, out, _ = run_cli("work", "--db", self.db, "--once", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"], {"claimed": False})

    def test_slots_full_reports_claimed_false(self):
        self.add("true", extra=("--queue", "gpu"))
        self.add("true", extra=("--queue", "gpu"))
        conn = store.connect(self.db)
        try:
            store.claim_next(conn, "w1", 111, time.time(), store.output_root(self.db),
                             lane="gpu")
        finally:
            conn.close()
        code, out, _ = run_cli("work", "--db", self.db, "--once", "--json",
                               "--queue", "gpu", "--slots", "1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"], {"claimed": False})

    def test_bad_work_queue_and_slots_rejected(self):
        code, out, _ = run_cli("work", "--db", self.db, "--once", "--json",
                               "--queue", "bad name")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")
        code, out, _ = run_cli("work", "--db", self.db, "--once", "--json",
                               "--slots", "0")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")


class TestDrain(WorkTestCase):
    def test_drain_once_mutually_exclusive(self):
        code, out, _ = run_cli("work", "--drain", "--once", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        e = json.loads(out)["errors"][0]
        self.assertEqual(e["code"], "INVALID_INPUT")
        self.assertIn("mutually exclusive", e["message"])

    def test_drain_empty_queue_exits_immediately(self):
        code, out, _ = run_cli("work", "--drain", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"], {"drained": True, "executed": 0})

    def test_drain_runs_everything_then_exits(self):
        for _ in range(3):
            self.add("sh", "-c", "true")
        code, out, _ = run_cli("work", "--drain", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"], {"drained": True, "executed": 3})

    def test_drain_waits_out_a_backoff_requeue(self):
        # Fails on the first execution, succeeds on the second: drain must
        # wait through the 2s backoff instead of exiting with work left.
        flaky = os.path.join(self.tmp.name, "flaky-marker")
        self.add("sh", "-c",
                 f"if [ -f {flaky} ]; then exit 0; else touch {flaky}; exit 1; fi")
        code, out, _ = run_cli("work", "--drain", "--db", self.db, "--json",
                               "--poll-interval", "0.05")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"], {"drained": True, "executed": 2})
        conn = store.connect(self.db)
        job = store.get_job(conn, 1)
        conn.close()
        self.assertEqual(job.state, "done")


class TestLoopMode(WorkTestCase):
    def test_loop_stdout_empty_and_sigterm_exits_zero(self):
        self.add("sh", "-c", "echo hi")
        self.add("sh", "-c", "echo hi 1>&2")
        proc = spawn_worker(self.db)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            conn = store.connect(self.db)
            n = conn.execute("SELECT COUNT(*) FROM jobs WHERE state='done'").fetchone()[0]
            conn.close()
            if n == 2:
                break
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(out, "", "loop mode must write nothing to stdout")
        self.assertIn("succeeded", err)

    def test_first_sigterm_finishes_in_flight_job(self):
        marker = os.path.join(self.tmp.name, "finished.txt")
        j = self.add("sh", "-c", f"sleep 1.5; echo done > {marker}")
        proc = spawn_worker(self.db)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.job_state(j) != "running":
            time.sleep(0.05)
        self.assertEqual(self.job_state(j), "running")
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.job_state(j), "done", err)
        self.assertTrue(os.path.exists(marker), "in-flight job must run to completion")

    def test_second_sigterm_kills_process_group(self):
        j = self.add("sh", "-c", "sleep 60")
        proc = spawn_worker(self.db)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.job_state(j) != "running":
            time.sleep(0.05)
        self.assertEqual(self.job_state(j), "running")
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0)
        self.assertLess(time.monotonic() - t0, 10, "second signal must not wait 60s")
        self.assertEqual(out, "")
        self.assertEqual(self.job_state(j), "queued", "killed job goes through normal retry")


if __name__ == "__main__":
    unittest.main()
