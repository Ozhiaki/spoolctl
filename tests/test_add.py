"""add verb grammar: argv fidelity, -c shell form, validation, envelope."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
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
        self.assertEqual(row["priority"], 0)
        self.assertEqual(row["queue"], "default")
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
        self.assertEqual(
            set(env["data"]),
            {"cwd", "deduplicated", "env_keys", "job_id", "next_run_at",
             "priority", "queue", "state"},
        )
        self.assertEqual(env["data"]["state"], "queued")
        self.assertIs(env["data"]["deduplicated"], False)
        self.assertEqual(env["data"]["priority"], 0)
        self.assertEqual(env["data"]["queue"], "default")
        self.assertIsNone(env["data"]["cwd"])
        self.assertEqual(env["data"]["env_keys"], [])

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


class TestExecutionFlags(AddTestCase):
    def test_cwd_resolved_absolute_at_submit(self):
        submit_dir = os.path.join(self.tmp.name, "submit")
        os.mkdir(submit_dir)
        old = os.getcwd()
        try:
            os.chdir(submit_dir)
            expected = os.path.abspath("relsub")
            code, out, _ = run_cli("add", "--db", self.db, "--json",
                                   "--cwd", "relsub", "--", "true")
        finally:
            os.chdir(old)
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        self.assertEqual(data["cwd"], expected)
        row = self.job_row(data["job_id"])
        self.assertEqual(row["cwd"], expected)

    def test_cwd_preserves_symlink_spelling(self):
        target = os.path.join(self.tmp.name, "target")
        link = os.path.join(self.tmp.name, "link")
        os.mkdir(target)
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")

        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--cwd", link, "--", "true")
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        self.assertEqual(data["cwd"], os.path.abspath(link))
        self.assertNotEqual(data["cwd"], os.path.realpath(link))
        row = self.job_row(data["job_id"])
        self.assertEqual(row["cwd"], os.path.abspath(link))

    def test_env_keys_sorted_and_values_not_echoed(self):
        code, out, _ = run_cli(
            "add", "--db", self.db, "--json",
            "--env", "TOKEN=secret", "--env", "A=1", "--env", "EMPTY=",
            "--env", "TOKEN=override",
            "--", "true",
        )
        self.assertEqual(code, 0)
        self.assertNotIn("secret", out)
        self.assertNotIn("override", out)
        data = json.loads(out)["data"]
        self.assertEqual(data["env_keys"], ["A", "EMPTY", "TOKEN"])
        row = self.job_row(data["job_id"])
        self.assertEqual(json.loads(row["env_json"]),
                         {"A": "1", "EMPTY": "", "TOKEN": "override"})

    def test_bad_execution_flags_rejected_before_db_creation(self):
        cases = [
            ("--cwd", ""),
            ("--cwd", "bad\x00cwd"),
            ("--env", "MISSING_EQUALS"),
            ("--env", "=value"),
            ("--env", "BAD\x00KEY=value"),
            ("--env", "A=bad\x00value"),
            ("--max-crashes", "-1"),
        ]
        for i, (flag, value) in enumerate(cases):
            with self.subTest(flag=flag, value=repr(value)):
                db = os.path.join(self.tmp.name, f"bad-exec-{i}.db")
                code, out, _ = run_cli("add", "--db", db, "--json",
                                       flag, value, "--", "true")
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")
                self.assertFalse(os.path.exists(db))


class TestIdempotencyKey(AddTestCase):
    def test_key_fresh_insert_then_active_dedup(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--key", "run-1", "--", "true")
        self.assertEqual(code, 0)
        first = json.loads(out)["data"]
        self.assertEqual(first["deduplicated"], False)
        self.assertEqual((first["job_id"], first["state"], first["priority"], first["queue"]),
                         (1, "queued", 0, "default"))

        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--key", "run-1", "--", "false")
        self.assertEqual(code, 0)
        second = json.loads(out)["data"]
        self.assertEqual(second["deduplicated"], True)
        self.assertEqual((second["job_id"], second["state"], second["priority"], second["queue"]),
                         (1, "queued", 0, "default"))

        conn = store.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"], 1)
        events = conn.execute("SELECT event FROM job_events ORDER BY id").fetchall()
        conn.close()
        self.assertEqual([e["event"] for e in events], ["added"])

    def test_dedup_returns_running_state(self):
        run_cli("add", "--db", self.db, "--json", "--key", "run-1", "--", "sleep", "5")
        conn = store.connect(self.db)
        store.claim_next(conn, "w", 1, time.time() + 1, store.output_root(self.db))
        conn.close()
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--key", "run-1", "--", "false")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"],
                         {
                             "deduplicated": True,
                             "cwd": None,
                             "env_keys": [],
                             "job_id": 1,
                             "next_run_at": self.job_row(1)["next_run_at"],
                             "priority": 0,
                             "queue": "default",
                             "state": "running",
                         })

    def test_dedup_returns_env_keys_without_values(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json", "--key", "run-1",
                               "--env", "TOKEN=secret", "--cwd", ".",
                               "--", "sleep", "5")
        self.assertEqual(code, 0)
        first = json.loads(out)["data"]
        conn = store.connect(self.db)
        store.claim_next(conn, "w", 1, time.time() + 1, store.output_root(self.db))
        conn.close()
        code, out, _ = run_cli("add", "--db", self.db, "--json", "--key", "run-1",
                               "--env", "TOKEN=other-secret",
                               "--", "false")
        self.assertEqual(code, 0)
        self.assertNotIn("secret", out)
        second = json.loads(out)["data"]
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(second["cwd"], first["cwd"])
        self.assertEqual(second["env_keys"], ["TOKEN"])

    def test_terminal_key_reuse_inserts_fresh_job(self):
        run_cli("add", "--db", self.db, "--json", "--max-retries", "0",
                "--key", "run-1", "--", "false")
        run_cli("work", "--once", "--db", self.db, "--json")
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--key", "run-1", "--", "true")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"],
                         {
                             "deduplicated": False,
                             "cwd": None,
                             "env_keys": [],
                             "job_id": 2,
                             "next_run_at": self.job_row(2)["next_run_at"],
                             "priority": 0,
                             "queue": "default",
                             "state": "queued",
                         })

    def test_retry_does_not_consult_active_key(self):
        run_cli("add", "--db", self.db, "--json", "--max-retries", "0",
                "--key", "run-1", "--", "false")
        run_cli("work", "--once", "--db", self.db, "--json")  # job 1 dead
        run_cli("add", "--db", self.db, "--json", "--key", "run-1", "--", "true")
        code, out, _ = run_cli("retry", "1", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"], {"job_id": 1, "state": "queued"})
        conn = store.connect(self.db)
        rows = conn.execute(
            "SELECT id FROM jobs WHERE idempotency_key='run-1'"
            " AND state IN ('queued','running') ORDER BY id"
        ).fetchall()
        conn.close()
        self.assertEqual([r["id"] for r in rows], [1, 2])


class TestSchedulingFlags(AddTestCase):
    def test_after_seconds_sets_future_next_run_at(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--after", "2s", "--", "true")
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        row = self.job_row(data["job_id"])
        self.assertAlmostEqual(row["next_run_at"] - row["created_at"], 2.0, delta=0.25)
        self.assertEqual(data["next_run_at"], row["next_run_at"])

    def test_after_fractional_minutes(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--after", "0.5m", "--", "true")
        self.assertEqual(code, 0)
        row = self.job_row(json.loads(out)["data"]["job_id"])
        self.assertAlmostEqual(row["next_run_at"] - row["created_at"], 30.0, delta=0.25)

    def test_at_past_clamps_to_created_at(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--at", "1", "--", "true")
        self.assertEqual(code, 0)
        row = self.job_row(json.loads(out)["data"]["job_id"])
        self.assertEqual(row["next_run_at"], row["created_at"])

    def test_at_future_epoch(self):
        future = time.time() + 3600
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--at", str(future), "--", "true")
        self.assertEqual(code, 0)
        row = self.job_row(json.loads(out)["data"]["job_id"])
        self.assertAlmostEqual(row["next_run_at"], future, delta=0.25)

    def test_priority_and_queue_stored(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--priority", "7", "--queue", "gpu.1", "--", "true")
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        row = self.job_row(data["job_id"])
        self.assertEqual((row["priority"], row["queue"]), (7, "gpu.1"))
        self.assertEqual((data["priority"], data["queue"]), (7, "gpu.1"))

    def test_after_and_at_are_mutually_exclusive(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--after", "1s", "--at", "1", "--", "true")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")

    def test_bad_after_values_rejected_before_db(self):
        cases = ["-1", "1H", "nan", "inf", "1e9999", "abc"]
        for i, raw in enumerate(cases):
            with self.subTest(raw=raw):
                db = os.path.join(self.tmp.name, f"bad-after-{i}.db")
                code, out, _ = run_cli("add", "--db", db, "--json",
                                       "--after", raw, "--", "true")
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")
                self.assertFalse(os.path.exists(db))

    def test_bad_at_values_rejected_before_db(self):
        cases = ["not-a-time", "nan", "inf", "1e9999"]
        for i, raw in enumerate(cases):
            with self.subTest(raw=raw):
                db = os.path.join(self.tmp.name, f"bad-at-{i}.db")
                code, out, _ = run_cli("add", "--db", db, "--json",
                                       "--at", raw, "--", "true")
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")
                self.assertFalse(os.path.exists(db))

    def test_bad_queue_values_rejected_before_db(self):
        cases = ["", " bad", "bad ", "bad name", "-bad", "x" * 65]
        for i, raw in enumerate(cases):
            with self.subTest(raw=repr(raw)):
                db = os.path.join(self.tmp.name, f"bad-queue-{i}.db")
                code, out, _ = run_cli("add", "--db", db, "--json",
                                       "--queue", raw, "--", "true")
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")
                self.assertFalse(os.path.exists(db))

    def test_bad_priority_values_rejected_before_db(self):
        cases = ["x", str(2_147_483_648), str(-2_147_483_649)]
        for i, raw in enumerate(cases):
            with self.subTest(raw=raw):
                db = os.path.join(self.tmp.name, f"bad-priority-{i}.db")
                code, out, _ = run_cli("add", "--db", db, "--json",
                                       "--priority", raw, "--", "true")
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")
                self.assertFalse(os.path.exists(db))

    def test_dedupe_returns_existing_scheduling_fields(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--key", "run-1", "--after", "10s",
                               "--priority", "5", "--queue", "gpu", "--", "true")
        self.assertEqual(code, 0)
        first = json.loads(out)["data"]
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--key", "run-1", "--after", "1s",
                               "--priority", "1", "--queue", "cpu", "--", "false")
        self.assertEqual(code, 0)
        second = json.loads(out)["data"]
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(second["next_run_at"], first["next_run_at"])
        self.assertEqual(second["priority"], 5)
        self.assertEqual(second["queue"], "gpu")
        row = self.job_row(first["job_id"])
        self.assertEqual((row["priority"], row["queue"]), (5, "gpu"))

    def test_key_is_stripped_before_store_and_lookup(self):
        run_cli("add", "--db", self.db, "--json", "--key", "  K  ", "--", "true")
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--key", "K", "--", "false")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["data"]["deduplicated"])
        row = self.job_row(1)
        self.assertEqual(row["idempotency_key"], "K")

    def test_bad_keys_rejected_before_db_creation(self):
        cases = ["   ", "x" * 257, "line\nbreak", "tab\tkey"]
        for i, key in enumerate(cases):
            with self.subTest(key=repr(key)):
                db = os.path.join(self.tmp.name, f"bad-{i}.db")
                code, out, _ = run_cli("add", "--db", db, "--json",
                                       "--key", key, "--", "true")
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")
                self.assertFalse(os.path.exists(db))


class TestTagsAndNotes(AddTestCase):
    def test_tags_and_note_persist_sorted(self):
        code, out, _ = run_cli(
            "add", "--db", self.db, "--json",
            "--tag", "b=2", "--tag", "a=1", "--note", "handoff",
            "--", "true",
        )
        self.assertEqual(code, 0)
        job_id = json.loads(out)["data"]["job_id"]
        row = self.job_row(job_id)
        self.assertEqual(row["tags_json"], '{"a": "1", "b": "2"}')
        self.assertEqual(row["note"], "handoff")

    def test_tag_value_may_be_empty(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--tag", "done=", "--", "true")
        self.assertEqual(code, 0)
        row = self.job_row(json.loads(out)["data"]["job_id"])
        self.assertEqual(row["tags_json"], '{"done": ""}')

    def test_bad_tag_and_note_inputs_rejected_before_db_creation(self):
        cases = [
            ("--tag", "missing-equals"),
            ("--tag", "=value"),
            ("--tag", "bad key=value"),
            ("--tag", "k" * 129 + "=value"),
            ("--tag", "k=" + "v" * 1025),
            ("--note", "n" * 10001),
        ]
        for i, (flag, value) in enumerate(cases):
            with self.subTest(flag=flag, value=value[:20]):
                db = os.path.join(self.tmp.name, f"bad-tag-{i}.db")
                code, out, _ = run_cli("add", "--db", db, "--json",
                                       flag, value, "--", "true")
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")
                self.assertFalse(os.path.exists(db))

    def test_duplicate_and_too_many_tags_rejected(self):
        code, out, _ = run_cli("add", "--db", self.db, "--json",
                               "--tag", "a=1", "--tag", "a=2", "--", "true")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")

        argv = ["add", "--db", self.db, "--json"]
        for i in range(17):
            argv += ["--tag", f"k{i}=v"]
        argv += ["--", "true"]
        code, out, _ = run_cli(*argv)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")

    def test_retry_preserves_key_tags_and_note(self):
        run_cli("add", "--db", self.db, "--json", "--max-retries", "0",
                "--key", "run-1", "--tag", "owner=agent", "--note", "handoff",
                "--", "false")
        run_cli("work", "--once", "--db", self.db, "--json")
        run_cli("retry", "1", "--db", self.db, "--json")
        code, out, _ = run_cli("show", "1", "--db", self.db, "--json")
        self.assertEqual(code, 0)
        job = json.loads(out)["data"]["job"]
        self.assertEqual(job["idempotency_key"], "run-1")
        self.assertEqual(job["tags"], {"owner": "agent"})
        self.assertEqual(job["note"], "handoff")


if __name__ == "__main__":
    unittest.main()
