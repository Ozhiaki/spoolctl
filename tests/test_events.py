"""events verb: cursored ledger reads, long-poll, and raw NDJSON follow."""

from __future__ import annotations

import io
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from spoolctl import cli, store


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class EventsTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def add(self, *extra: str) -> int:
        code, out, err = run_cli("add", "--db", self.db, "--json", *extra, "--", "true")
        self.assertEqual(code, 0, err)
        return json.loads(out)["data"]["job_id"]

    def events(self, *extra: str) -> dict:
        code, out, err = run_cli("events", "--db", self.db, "--json", *extra)
        self.assertEqual(code, 0, err)
        env = json.loads(out)
        return env

    def read_follow_line(self, proc: subprocess.Popen[str], timeout: float = 3.0) -> str:
        assert proc.stdout is not None
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        self.assertTrue(ready, "timed out waiting for follow output")
        return proc.stdout.readline()

    def start_follow(self, *extra: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "spoolctl.cli",
                "events",
                "--db",
                self.db,
                "--json",
                "--follow",
                "--poll-interval",
                "0.02",
                *extra,
            ],
            cwd=os.getcwd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def stop_follow(self, proc: subprocess.Popen[str]) -> None:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()


class TestEventsOneShot(EventsTestCase):
    def test_one_shot_returns_ascending_events_and_pagination(self):
        self.add()
        self.add()
        env = self.events()
        self.assertEqual([e["id"] for e in env["data"]["events"]], [1, 2])
        self.assertEqual([e["event"] for e in env["data"]["events"]], ["added", "added"])
        self.assertEqual(env["data"]["count"], 2)
        self.assertEqual(env["meta"]["pagination"], {"cursor": 2, "first_id": 1})

    def test_since_job_and_limit_compose_without_job_existence_check(self):
        first = self.add()
        second = self.add()
        env = self.events("--since-id", "0", "--job", str(second), "--limit", "1")
        self.assertEqual([e["job_id"] for e in env["data"]["events"]], [second])
        self.assertEqual(env["meta"]["pagination"]["cursor"], 2)

        env = self.events("--job", "999")
        self.assertEqual(env["data"], {"count": 0, "events": []})
        self.assertEqual(env["meta"]["pagination"], {"cursor": 2, "first_id": None})
        self.assertNotEqual(first, second)

    def test_empty_result_cursor_never_rewinds(self):
        self.add()
        env = self.events("--since-id", "100")
        self.assertEqual(env["data"]["events"], [])
        self.assertEqual(env["meta"]["pagination"]["cursor"], 100)

    def test_limit_plus_one_distinguishes_exact_limit_from_truncation(self):
        self.add()
        self.add()
        env = self.events("--limit", "2")
        self.assertEqual([e["id"] for e in env["data"]["events"]], [1, 2])
        self.assertEqual(env["meta"]["pagination"]["cursor"], 2)

        self.add()
        env = self.events("--limit", "2")
        self.assertEqual([e["id"] for e in env["data"]["events"]], [1, 2])
        self.assertEqual(env["meta"]["pagination"]["cursor"], 2)
        env = self.events("--since-id", "2")
        self.assertEqual([e["id"] for e in env["data"]["events"]], [3])

    def test_first_id_reports_retained_history_after_prune(self):
        self.add("--max-retries", "0")
        run_cli("work", "--once", "--db", self.db, "--json")
        self.add()
        run_cli("prune", "--older-than", "0", "--db", self.db, "--json")
        env = self.events("--since-id", "0")
        self.assertEqual([e["id"] for e in env["data"]["events"]], [4])
        self.assertEqual(env["meta"]["pagination"]["first_id"], 4)


class TestEventsWait(EventsTestCase):
    def test_wait_timeout_success_empty_with_non_rewinding_cursor(self):
        self.add()
        env = self.events(
            "--since-id", "1",
            "--wait",
            "--wait-timeout", "0.05",
            "--poll-interval", "0.01",
        )
        self.assertEqual(env["data"], {"count": 0, "events": []})
        self.assertEqual(env["meta"]["pagination"]["cursor"], 1)
        self.assertEqual(env["meta"]["wait"]["reason"], "timeout")

    def test_wait_returns_when_matching_record_arrives(self):
        def add_later():
            time.sleep(0.05)
            conn = store.connect(self.db)
            try:
                store.add_job(conn, ["true"], 300, 3, time.time())
            finally:
                conn.close()

        thread = threading.Thread(target=add_later)
        thread.start()
        env = self.events(
            "--wait",
            "--wait-timeout", "2",
            "--poll-interval", "0.01",
        )
        thread.join(timeout=2)
        self.assertEqual(env["data"]["count"], 1)
        self.assertEqual(env["data"]["events"][0]["event"], "added")
        self.assertEqual(env["meta"]["wait"]["reason"], "records_available")

    def test_invalid_follow_combinations_and_bounds_fail(self):
        cases = [
            ("--follow", "--limit", "1"),
            ("--follow", "--wait"),
            ("--since-id", "-1"),
            ("--limit", "-1"),
            ("--wait-timeout", "0"),
            ("--poll-interval", "0"),
            ("--job", "0"),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, _ = run_cli("events", "--db", self.db, "--json", *argv)
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")


class TestEventsFollow(EventsTestCase):
    def test_follow_json_emits_bare_ndjson_records_and_flushes(self):
        self.add()
        proc = self.start_follow("--since-id", "0")
        self.addCleanup(self.stop_follow, proc)
        first = json.loads(self.read_follow_line(proc))
        self.assertNotIn("ok", first)
        self.assertNotIn("data", first)
        self.assertEqual(first["id"], 1)

        self.add()
        second = json.loads(self.read_follow_line(proc))
        self.assertEqual(second["id"], 2)
        self.assertEqual(second["event"], "added")

    def test_follow_defaults_to_now_unless_since_id_is_given(self):
        self.add()
        proc = self.start_follow()
        self.addCleanup(self.stop_follow, proc)
        time.sleep(0.1)
        self.add()
        event = json.loads(self.read_follow_line(proc))
        self.assertEqual(event["id"], 2)

    def test_follow_tolerates_prune_induced_gaps(self):
        self.add("--max-retries", "0")
        run_cli("work", "--once", "--db", self.db, "--json")
        self.add()
        run_cli("prune", "--older-than", "0", "--db", self.db, "--json")
        proc = self.start_follow("--since-id", "0")
        self.addCleanup(self.stop_follow, proc)
        event = json.loads(self.read_follow_line(proc))
        self.assertEqual(event["id"], 4)
        self.assertEqual(event["event"], "added")


if __name__ == "__main__":
    unittest.main()
