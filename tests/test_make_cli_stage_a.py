"""Stage A make-cli conformance probes for current high-risk input cells."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from spoolctl import cli


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class StageAProbeTestCase(unittest.TestCase):
    maxDiff = None

    def assert_json_failure(
        self,
        argv: tuple[str, ...],
        expected_code: str,
        *,
        exit_code: int = 1,
    ) -> dict:
        code, out, err = run_cli(*argv)
        self.assertEqual(code, exit_code, argv)
        self.assertTrue(out.strip(), f"empty stdout for {argv}")
        env = json.loads(out)
        self.assertFalse(env["ok"], argv)
        self.assertIsNone(env["data"], argv)
        self.assertEqual(env["errors"][0]["code"], expected_code, argv)
        self.assertEqual(env["errors"][0]["exit_code"], exit_code, argv)
        self.assertIn(env["errors"][0]["message"], err, argv)
        self.assertNotIn("Traceback", err, argv)
        return env


class TestParserStageA(StageAProbeTestCase):
    def test_flag_abbreviations_are_not_accepted(self):
        code, out, err = run_cli("status", "--jso")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("unknown flag: --jso", err)
        self.assertNotIn("Traceback", err)
        self.assert_json_failure(("list", "--lim", "3", "--json"), "UNKNOWN_FLAG")

    def test_enum_typo_is_invalid_input_not_unknown_command(self):
        self.assert_json_failure(
            ("output", "--json", "1", "--stream", "stdou"),
            "INVALID_INPUT",
        )

    def test_json_equals_parse_failure_still_returns_envelope(self):
        self.assert_json_failure(("status", "--json=1", "--bogus"), "INVALID_INPUT")

    def test_missing_and_bad_verbless_invocations(self):
        code, out, err = run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("missing required verb", err)

        self.assert_json_failure(("--json",), "MISSING_REQUIRED")
        self.assert_json_failure(("--bogus", "--json"), "UNKNOWN_FLAG")
        self.assert_json_failure(("--json", "--bogus"), "UNKNOWN_FLAG")

    def test_db_free_verbs_reject_db_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "missing", "queue.db")
            for verb in ("brief", "schema"):
                with self.subTest(verb=verb):
                    self.assert_json_failure((verb, "--json", "--db", db), "UNKNOWN_FLAG")
                    self.assertFalse(os.path.exists(db))
                    self.assertFalse(os.path.exists(os.path.dirname(db)))

    def test_add_misplaced_spoolctl_flags_do_not_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "queue.db")
            self.assert_json_failure(
                ("add", "--db", db, "--json", "echo", "hi", "--json"),
                "INVALID_INPUT",
            )
            self.assertFalse(os.path.exists(db))

            code, out, err = run_cli(
                "add", "--db", db, "--json", "--", "echo", "hi", "--json"
            )
            self.assertEqual(code, 0, err)
            self.assertTrue(json.loads(out)["ok"])


class TestNumericStageA(StageAProbeTestCase):
    HUGE = "99999999999999999999"
    TOO_BIG = "9223372036854775808"
    TOO_SMALL = "-9223372036854775809"

    def test_huge_integer_positionals_are_structured_input_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "queue.db")
            for verb in ("show", "cancel", "retry", "output"):
                with self.subTest(verb=verb):
                    self.assert_json_failure(
                        (verb, self.HUGE, "--db", db, "--json"),
                        "INVALID_INPUT",
                    )
            self.assert_json_failure(
                ("wait", "1", self.HUGE, "--db", db, "--json"),
                "INVALID_INPUT",
            )
            self.assertFalse(os.path.exists(db))

    def test_integer_flags_reject_int64_overflow_before_db(self):
        int_flag_rows = [
            (("add",), "--timeout", ("--", "true")),
            (("add",), "--max-retries", ("--", "true")),
            (("add",), "--max-crashes", ("--", "true")),
            (("status",), "--limit", ()),
            (("list",), "--limit", ()),
            (("list",), "--priority-min", ()),
            (("work", "--once"), "--slots", ()),
            (("events",), "--since-id", ()),
            (("events",), "--limit", ()),
            (("events",), "--job", ()),
            (("output", "1"), "--attempt", ()),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for i, (prefix, flag, tail) in enumerate(int_flag_rows):
                db = os.path.join(tmp, f"bad-{i}.db")
                argv = (*prefix, "--db", db, "--json", flag, self.TOO_BIG, *tail)
                with self.subTest(argv=argv):
                    self.assert_json_failure(argv, "INVALID_INPUT")
                    self.assertFalse(os.path.exists(db))

    def test_float_flags_reject_nan_inf_and_huge_before_db(self):
        float_flag_rows = [
            ("wait", "1", "--timeout"),
            ("wait", "1", "--poll-interval"),
            ("work", "--once", "--poll-interval"),
            ("events", "--wait", "--wait-timeout"),
            ("events", "--wait", "--poll-interval"),
        ]
        bad_values = ("nan", "-nan", "inf", "-inf", "1e309")
        with tempfile.TemporaryDirectory() as tmp:
            for i, row in enumerate(float_flag_rows):
                for bad in bad_values:
                    db = os.path.join(tmp, f"bad-float-{i}-{bad}.db")
                    verb, *prefix, flag = row
                    argv = (verb, "--db", db, "--json", *prefix, flag, bad)
                    with self.subTest(argv=argv):
                        self.assert_json_failure(argv, "INVALID_INPUT")
                        self.assertFalse(os.path.exists(db))


class TestDbPathStageA(StageAProbeTestCase):
    def test_bad_db_paths_are_structured_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent_file = os.path.join(tmp, "parent-file")
            with open(parent_file, "w", encoding="utf-8") as f:
                f.write("not a directory")

            for argv in (
                ("status", "--db", os.path.join(parent_file, "queue.db"), "--json"),
                ("status", "--json"),
            ):
                with self.subTest(argv=argv):
                    old = os.environ.get("SPOOLCTL_DB")
                    if argv == ("status", "--json"):
                        os.environ["SPOOLCTL_DB"] = os.path.join(parent_file, "env.db")
                    try:
                        self.assert_json_failure(argv, "INVALID_INPUT")
                    finally:
                        if old is None:
                            os.environ.pop("SPOOLCTL_DB", None)
                        else:
                            os.environ["SPOOLCTL_DB"] = old


if __name__ == "__main__":
    unittest.main()
