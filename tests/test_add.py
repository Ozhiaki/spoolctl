"""add verb grammar: argv fidelity, -c shell form, validation, envelope."""

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


class AddTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def stored_argv(self, job_id: int) -> list[str]:
        conn = store.connect(self.db)
        row = conn.execute("SELECT argv_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        return json.loads(row["argv_json"])

    def job_row(self, job_id: int):
        conn = store.connect(self.db)
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
        return row


class TestArgvForm(AddTestCase):
    def test_quoted_arg_survives_byte_for_byte(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json", "--",
                               "echo", "hello && goodbye")
        self.assertEqual(code, 0)
        job_id = json.loads(out)["data"]["job_id"]
        self.assertEqual(self.stored_argv(job_id), ["echo", "hello && goodbye"])

    def test_without_double_dash(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json", "echo", "hi")
        self.assertEqual(code, 0)
        job_id = json.loads(out)["data"]["job_id"]
        self.assertEqual(self.stored_argv(job_id), ["echo", "hi"])

    def test_job_flags_after_command_stay_with_job(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json", "ls", "--color=never")
        self.assertEqual(code, 0)
        job_id = json.loads(out)["data"]["job_id"]
        self.assertEqual(self.stored_argv(job_id), ["ls", "--color=never"])


class TestShellForm(AddTestCase):
    def test_c_stores_sh_dash_c_array(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "-c", "echo a && echo b")
        self.assertEqual(code, 0)
        job_id = json.loads(out)["data"]["job_id"]
        self.assertEqual(self.stored_argv(job_id), ["sh", "-c", "echo a && echo b"])

    def test_c_plus_positionals_rejected_with_corrected_command(self):
        code, out, err = run_cli("add", "--db", self.db, "--json", "-c", "echo", "hi")
        self.assertEqual(code, 1)
        e = json.loads(out)["errors"][0]
        self.assertEqual(e["code"], "INVALID_INPUT")
        self.assertIn("spoolctl add -c 'echo hi'", e["remediation"])

    def test_empty_c_string_rejected(self):
        code, _, err = run_cli("add", "--db", self.db, "-c", "  ")
        self.assertEqual(code, 1)
        self.assertIn("spoolctl add", err)


class TestValidation(AddTestCase):
    def test_no_command_shows_both_forms(self):
        code, out, err = run_cli("add", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        e = json.loads(out)["errors"][0]
        self.assertEqual(e["code"], "MISSING_REQUIRED")
        self.assertIn("add --", e["remediation"])
        self.assertIn("add -c", e["remediation"])
        self.assertTrue(err.strip())

    def test_timeout_zero_rejected_before_db(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--timeout", "0", "--", "true")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")
        self.assertFalse(os.path.exists(self.db), "validation must precede DB creation")

    def test_negative_max_retries_rejected(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--max-retries", "-1", "--", "true")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")


class TestRowAndOutput(AddTestCase):
    def test_row_defaults_and_event(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json", "--", "true")
        job_id = json.loads(out)["data"]["job_id"]
        row = self.job_row(job_id)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["max_retries"], 3)
        self.assertEqual(row["timeout_seconds"], 300)
        self.assertEqual(row["next_run_at"], row["created_at"])
        conn = store.connect(self.db)
        events = [r["event"] for r in conn.execute(
            "SELECT event FROM job_events WHERE job_id=?", (job_id,))]
        conn.close()
        self.assertEqual(events, ["added"])

    def test_custom_timeout_and_retries_stored(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--timeout", "7", "--max-retries", "0", "--", "true")
        row = self.job_row(json.loads(out)["data"]["job_id"])
        self.assertEqual((row["timeout_seconds"], row["max_retries"]), (7, 0))

    def test_human_mode_prints_added_job(self):
        code, out, _ = run_cli("add", "--db", self.db, "--", "true")
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^Added job \d+$")

    def test_envelope_data_shape(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json", "--", "true")
        env = json.loads(out)
        self.assertTrue(env["ok"])
        self.assertEqual(set(env["data"]), {"deduplicated", "job_id", "state"})
        self.assertEqual(env["data"]["state"], "queued")
        self.assertIs(env["data"]["deduplicated"], False)

    def test_env_var_db_path(self):
        envdb = os.path.join(self.tmp.name, "env.db")
        old = os.environ.get("SPOOLCTL_DB")
        os.environ["SPOOLCTL_DB"] = envdb
        try:
            code, out, _ = run_cli("add", "--json", "--", "true")
        finally:
            if old is None:
                os.environ.pop("SPOOLCTL_DB", None)
            else:
                os.environ["SPOOLCTL_DB"] = old
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(envdb))


if __name__ == "__main__":
    unittest.main()
