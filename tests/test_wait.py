"""wait verb: exit paths (0/6/4/1), the ok:true exit-6 exception, grammar."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from spoolctl import cli, store


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class WaitTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def add(self, *cmd: str, max_retries: int = 3) -> int:
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--max-retries", str(max_retries), "--", *cmd)
        assert code == 0
        return json.loads(out)["data"]["job_id"]

    def work_all(self):
        while True:
            _, out, _ = run_cli("work", "--once", "--db", self.db, "--json")
            if not json.loads(out)["data"]["claimed"]:
                return


class TestWaitExitPaths(WaitTestCase):
    def test_all_done_exit_0(self):
        ids = [self.add("true"), self.add("true")]
        self.work_all()
        code, out, _ = run_cli("wait", *map(str, ids), "--db", self.db, "--json")
        self.assertEqual(code, 0)
        env = json.loads(out)
        self.assertTrue(env["data"]["all_succeeded"])
        self.assertEqual(
            {k: v["state"] for k, v in env["data"]["jobs"].items()},
            {str(i): "done" for i in ids},
        )

    def test_any_dead_exit_6_with_ok_true(self):
        good = self.add("true")
        bad = self.add("false", max_retries=0)
        self.work_all()
        code, out, _ = run_cli("wait", str(good), str(bad), "--db", self.db, "--json")
        self.assertEqual(code, 6)
        env = json.loads(out)
        # the documented exception: tool call succeeded, exit carries outcome
        self.assertIs(env["ok"], True)
        self.assertEqual(env["errors"], [])
        self.assertFalse(env["data"]["all_succeeded"])
        self.assertEqual(env["data"]["jobs"][str(bad)]["state"], "dead")
        self.assertEqual(env["data"]["jobs"][str(bad)]["last_error"], "exit 1")

    def test_canceled_counts_as_not_succeeded(self):
        job_id = self.add("true")
        run_cli("cancel", str(job_id), "--db", self.db, "--json")
        code, out, _ = run_cli("wait", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 6)
        self.assertEqual(
            json.loads(out)["data"]["jobs"][str(job_id)]["state"], "canceled")

    def test_timeout_exit_4(self):
        job_id = self.add("true")  # queued, no worker: never settles
        code, out, _ = run_cli("wait", str(job_id), "--db", self.db, "--json",
                               "--timeout", "0.2", "--poll-interval", "0.05")
        self.assertEqual(code, 4)
        e = json.loads(out)["errors"][0]
        self.assertEqual(e["code"], "TIMEOUT")
        self.assertIn(f"spoolctl wait --timeout 0.2 {job_id}", e["remediation"])

    def test_unknown_ids_fail_fast_exit_1(self):
        known = self.add("true")
        code, out, _ = run_cli("wait", str(known), "41", "42",
                               "--db", self.db, "--json")
        self.assertEqual(code, 1)
        e = json.loads(out)["errors"][0]
        self.assertEqual(e["code"], "NOT_FOUND")
        self.assertIn("41, 42", e["message"])


class TestWaitGrammar(WaitTestCase):
    def test_no_ids_missing_required(self):
        code, out, _ = run_cli("wait", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "MISSING_REQUIRED")

    def test_non_integer_id_invalid_input(self):
        code, out, _ = run_cli("wait", "abc", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")

    def test_nonpositive_timeout_rejected(self):
        code, out, _ = run_cli("wait", "1", "--db", self.db, "--json",
                               "--timeout", "0")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")

    def test_nonpositive_poll_interval_rejected(self):
        code, out, _ = run_cli("wait", "1", "--db", self.db, "--json",
                               "--poll-interval", "-1")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")

    def test_duplicate_ids_tolerated(self):
        job_id = self.add("true")
        self.work_all()
        code, out, _ = run_cli("wait", str(job_id), str(job_id),
                               "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["data"]["jobs"]), 1)


class TestWaitDeterminism(WaitTestCase):
    def test_identical_settled_queue_identical_data_hash(self):
        ids = [self.add("true"), self.add("false", max_retries=0)]
        self.work_all()
        _, out1, _ = run_cli("wait", *map(str, ids), "--db", self.db, "--json")
        _, out2, _ = run_cli("wait", *map(str, ids), "--db", self.db, "--json")
        self.assertEqual(json.loads(out1)["meta"]["data_hash"],
                         json.loads(out2)["meta"]["data_hash"])


if __name__ == "__main__":
    unittest.main()
