"""Contract suite: per-verb envelope goldens, remaining exit-code paths,
executed argv-vs-shell fidelity, backoff cap, end-to-end flow.

Golden files live in tests/golden/envelope-<verb>.json. To re-pin after an
intentional contract change: SPOOLCTL_REPIN=1 python -m unittest
tests.test_contract, then commit the diff with a changelog entry.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from spoolctl import cli, models, store

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
REPIN = os.environ.get("SPOOLCTL_REPIN") == "1"


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def normalize_envelope(env: dict, tmp_root: str) -> dict:
    """Blank the call-specific meta facts and tmp paths so the rest pins."""
    env = json.loads(json.dumps(env))
    env["meta"]["request_id"] = "req_PINNED"
    env["meta"]["ts_iso"] = "PINNED"
    env["meta"]["elapsed_ms"] = 0
    env["meta"]["data_hash"] = "sha256:PINNED"

    def walk(node):
        if isinstance(node, str):
            return node.replace(os.path.realpath(tmp_root), "<TMP>").replace(tmp_root, "<TMP>")
        if isinstance(node, list):
            return [walk(x) for x in node]
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        return node

    return walk(env)


class GoldenEnvelopeTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def assert_golden(self, name: str, raw_json: str):
        env = normalize_envelope(json.loads(raw_json), self.tmp.name)
        got = json.dumps(env, indent=2, sort_keys=True) + "\n"
        path = GOLDEN_DIR / f"envelope-{name}.json"
        if REPIN:
            path.write_text(got)
            return
        self.assertTrue(path.exists(), f"missing golden {path}; run with SPOOLCTL_REPIN=1")
        self.assertEqual(
            got, path.read_text(),
            f"\n\nenvelope for {name!r} drifted from {path.name}. If intentional:"
            " SPOOLCTL_REPIN=1 re-pin + changelog entry.\n",
        )


class TestPerVerbEnvelopeGoldens(GoldenEnvelopeTestCase):
    def test_add(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json", "--", "echo", "hi")
        self.assertEqual(code, 0)
        self.assert_golden("add", out)

    def test_work_once_empty(self):
        code, out, _ = run_cli("work", "--once", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assert_golden("work-once-empty", out)

    def test_work_drain_empty(self):
        code, out, _ = run_cli("work", "--drain", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assert_golden("work-drain-empty", out)

    def test_status_empty(self):
        code, out, _ = run_cli("status", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assert_golden("status-empty", out)

    def test_list_empty(self):
        code, out, _ = run_cli("list", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assert_golden("list-empty", out)

    def test_show_unknown_id_error_envelope(self):
        code, out, _ = run_cli("show", "42", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        self.assert_golden("show-not-found", out)

    def test_wait_mixed_outcome_exit_6_ok_true(self):
        run_cli("add", "--db", self.db, "--json", "--", "true")
        run_cli("add", "--db", self.db, "--json", "--max-retries", "0", "--", "false")
        run_cli("work", "--once", "--db", self.db, "--json")
        run_cli("work", "--once", "--db", self.db, "--json")
        code, out, _ = run_cli("wait", "1", "2", "--db", self.db, "--json")
        self.assertEqual(code, 6)
        self.assert_golden("wait-mixed", out)

    def test_prune_empty(self):
        code, out, _ = run_cli("prune", "--older-than", "0", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assert_golden("prune-empty", out)

    def test_cancel_queued(self):
        run_cli("add", "--db", self.db, "--json", "--", "sleep", "9")
        code, out, _ = run_cli("cancel", "1", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assert_golden("cancel-queued", out)

    def test_retry_unknown_id_error_envelope(self):
        code, out, _ = run_cli("retry", "42", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        self.assert_golden("retry-not-found", out)

    def test_output_no_attempts(self):
        run_cli("add", "--db", self.db, "--json", "--", "echo", "hi")
        code, out, _ = run_cli("output", "1", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assert_golden("output-no-attempts", out)

    def test_events_empty(self):
        code, out, _ = run_cli("events", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assert_golden("events-empty", out)

    def test_brief(self):
        code, out, _ = run_cli("brief", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assert_golden("brief", out)

    def test_schema(self):
        code, out, _ = run_cli("schema", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assert_golden("schema", out)


class TestRemainingExitCodePaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def test_db_busy_beyond_timeout_is_locked_exit_4(self):
        blocker = store.connect(self.db)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        self.addCleanup(lambda: blocker.execute("ROLLBACK"))
        with mock.patch.object(store, "BUSY_TIMEOUT_MS", 100):
            code, out, err = run_cli("add", "--db", self.db, "--json", "--", "true")
        self.assertEqual(code, 4)
        e = json.loads(out)["errors"][0]
        self.assertEqual(e["code"], "LOCKED")
        self.assertIn("retry after a few seconds", e["remediation"])
        self.assertTrue(err.strip())


class TestBackoffCap(unittest.TestCase):
    def test_backoff_sequence_caps_at_60(self):
        self.assertEqual(
            [models.backoff_seconds(n) for n in range(1, 8)],
            [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0],
        )


class TestExecutedCommandFidelity(unittest.TestCase):
    """The pair of executions behind ruling R1: argv is literal, -c is shell."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def run_and_read(self) -> str:
        code, out, err = run_cli("work", "--once", "--db", self.db, "--json")
        data = json.loads(out)["data"]
        assert data["result"] == "succeeded", (data, err)
        code, out, _ = run_cli("output", str(data["job_id"]), "--db", self.db,
                               "--stream", "stdout")
        return "\n".join(out.splitlines()[1:])  # drop the header line

    def test_argv_form_echoes_ampersands_literally(self):
        run_cli("add", "--db", self.db, "--json", "--", "echo", "hello && goodbye")
        self.assertEqual(self.run_and_read(), "hello && goodbye")

    def test_c_form_runs_the_shell(self):
        run_cli("add", "--db", self.db, "--json", "-c", "echo a && echo b")
        self.assertEqual(self.run_and_read(), "a\nb")


class TestEndToEndFlow(unittest.TestCase):
    """add -> work -> output -> retry -> work: the whole loop in one test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def test_full_lifecycle(self):
        # add a job that fails on its only allowed execution
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--max-retries", "0", "-c", "printf attempt; exit 1")
        job_id = json.loads(out)["data"]["job_id"]

        # work: executes and dead-letters it
        code, out, _ = run_cli("work", "--once", "--db", self.db, "--json")
        data = json.loads(out)["data"]
        self.assertEqual((code, data["result"], data["job_state"]), (0, "failed", "dead"))

        # status: shows it dead
        _, out, _ = run_cli("status", "--db", self.db, "--json")
        sdata = json.loads(out)["data"]
        self.assertEqual(sdata["counts"]["dead"], 1)
        self.assertEqual(sdata["recent_dead"][0]["id"], job_id)

        # output: captured the failing attempt
        _, out, _ = run_cli("output", str(job_id), "--db", self.db, "--json")
        self.assertEqual(json.loads(out)["data"]["streams"]["stdout"]["preview"],
                         "attempt")

        # retry: fresh budget, then make it succeed and work it again
        code, _, _ = run_cli("retry", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 0)
        conn = store.connect(self.db)
        conn.execute("UPDATE jobs SET argv_json=? WHERE id=?",
                     ('["sh","-c","printf recovered"]', job_id))
        conn.close()
        code, out, _ = run_cli("work", "--once", "--db", self.db, "--json")
        data = json.loads(out)["data"]
        self.assertEqual((data["result"], data["job_state"], data["attempt_no"]),
                         ("succeeded", "done", 2))

        # output default follows the latest attempt
        _, out, _ = run_cli("output", str(job_id), "--db", self.db,
                            "--stream", "stdout")
        self.assertIn("recovered", out)


if __name__ == "__main__":
    unittest.main()
