"""CLI framework: envelope shape, determinism, did-you-mean, exit codes."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from spoolctl import cli
from spoolctl.models import EXIT_CODES

REPO = Path(__file__).resolve().parent.parent

ENVELOPE_KEYS = {"ok", "tool_version", "data", "meta", "warnings", "commands", "errors"}
META_KEYS = {"request_id", "ts_iso", "elapsed_ms", "contract_version", "data_hash"}


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestEnvelopeShape(unittest.TestCase):
    def test_all_seven_keys_always_present_on_failure(self):
        code, out, err = run_cli("statu", "--json")
        env = json.loads(out)
        self.assertEqual(set(env), ENVELOPE_KEYS)
        self.assertEqual(set(env["meta"]), META_KEYS)
        self.assertFalse(env["ok"])
        self.assertIsNone(env["data"])
        self.assertEqual(env["warnings"], [])
        self.assertEqual(env["commands"], [])
        self.assertEqual(len(env["errors"]), 1)
        self.assertNotEqual(code, 0)

    def test_data_hash_deterministic_for_identical_data(self):
        h1 = cli.canonical_data_hash({"b": 1, "a": [2, 3]})
        h2 = cli.canonical_data_hash({"a": [2, 3], "b": 1})
        self.assertEqual(h1, h2)
        self.assertTrue(h1.startswith("sha256:"))

    def test_envelope_success_shape(self):
        env = cli.make_envelope({"x": 1}, started=0.0)
        self.assertEqual(set(env), ENVELOPE_KEYS)
        self.assertTrue(env["ok"])
        self.assertEqual(env["errors"], [])
        self.assertTrue(env["meta"]["ts_iso"].endswith("Z"))
        self.assertTrue(env["meta"]["request_id"].startswith("req_"))


class TestDidYouMean(unittest.TestCase):
    def test_typoed_verb_suggests_and_shows_corrected_command(self):
        code, out, err = run_cli("statu", "--json")
        env = json.loads(out)
        e = env["errors"][0]
        self.assertEqual(e["code"], "UNKNOWN_COMMAND")
        self.assertEqual(e["did_you_mean"], "status")
        self.assertIn("spoolctl status", e["remediation"])
        self.assertEqual(code, 1)
        self.assertIn("statu", err)

    def test_typoed_flag_suggests_and_shows_corrected_command(self):
        code, out, err = run_cli("status", "--jsn")
        self.assertEqual(code, 1)
        self.assertIn("did you mean '--json'", err)
        self.assertIn("spoolctl status --json", err)

    def test_unknown_gibberish_verb_no_suggestion(self):
        code, out, err = run_cli("zzqqz", "--json")
        env = json.loads(out)
        self.assertEqual(env["errors"][0]["code"], "UNKNOWN_COMMAND")
        self.assertNotIn("did_you_mean", env["errors"][0])
        self.assertEqual(code, 1)

    def test_levenshtein_leq1(self):
        self.assertTrue(cli._levenshtein_leq1("statu", "status"))
        self.assertTrue(cli._levenshtein_leq1("statuz", "status"))
        self.assertTrue(cli._levenshtein_leq1("status", "status"))
        self.assertFalse(cli._levenshtein_leq1("stat", "status"))
        self.assertFalse(cli._levenshtein_leq1("wrok", "work"))  # transposition = 2 edits


class TestFailureDiscipline(unittest.TestCase):
    def test_every_failure_writes_stderr_and_nonzero_exit(self):
        for argv in (["statu"], ["status", "--jsn"], ["retry"]):
            code, out, err = run_cli(*argv)
            self.assertNotEqual(code, 0, argv)
            self.assertTrue(err.strip(), f"no stderr for {argv}")

    def test_exit_codes_come_from_dictionary(self):
        for argv in (["statu"], ["status", "--jsn"], ["retry"], ["capabilities"]):
            code, _, _ = run_cli(*argv)
            self.assertIn(code, EXIT_CODES, argv)

    def test_json_failure_mirrors_message_to_stderr(self):
        code, out, err = run_cli("statu", "--json")
        env = json.loads(out)
        self.assertIn(env["errors"][0]["message"].split(":")[0], err)

    def test_missing_required_argument(self):
        code, out, err = run_cli("retry", "--json")
        env = json.loads(out)
        self.assertEqual(env["errors"][0]["code"], "MISSING_REQUIRED")
        self.assertEqual(code, 1)


class TestHumanMode(unittest.TestCase):
    def test_no_ansi_color_in_help_or_errors(self):
        proc = subprocess.run(
            [sys.executable, "-m", "spoolctl", "--help"],
            cwd=REPO, capture_output=True, text=True,
        )
        self.assertNotIn("\x1b[", proc.stdout + proc.stderr)
        _, out, err = run_cli("statu")
        self.assertNotIn("\x1b[", out + err)

    def test_no_verb_prints_help_exit_zero(self):
        code, out, _ = run_cli()
        self.assertEqual(code, 0)
        self.assertIn("VERB", out)


if __name__ == "__main__":
    unittest.main()
