"""Closed error/warning code registry coverage."""

from __future__ import annotations

import ast
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from spoolctl import cli, store
from spoolctl.models import CODE_REGISTRY, ERROR_CODES, WARNING_CODES

REPO = Path(__file__).resolve().parent.parent


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestRegistryShape(unittest.TestCase):
    def test_error_and_warning_views_are_derived_from_registry(self):
        expected_errors = {
            code for code, entry in CODE_REGISTRY.items()
            if "errors" in entry["appears_in"]
        }
        expected_warnings = {
            code for code, entry in CODE_REGISTRY.items()
            if "warnings" in entry["appears_in"]
        }
        self.assertEqual(set(ERROR_CODES), expected_errors)
        self.assertEqual(set(WARNING_CODES), expected_warnings)

    def test_capabilities_exposes_closed_registry(self):
        code, out, err = run_cli("capabilities", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        self.assertEqual(data["code_registry"], CODE_REGISTRY)
        self.assertEqual(set(data["error_codes"]), set(ERROR_CODES))


class TestStaticRegistryCoverage(unittest.TestCase):
    def test_cli_error_literals_are_registered_for_errors(self):
        tree = ast.parse((REPO / "spoolctl" / "cli.py").read_text())
        found: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "CliError":
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    found.add(node.args[0].value)
        unknown = found - set(ERROR_CODES)
        self.assertFalse(unknown, f"unregistered error codes: {sorted(unknown)}")

    def test_warning_code_literals_are_registered_for_warnings(self):
        tree = ast.parse((REPO / "spoolctl" / "cli.py").read_text())
        found: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "code"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    found.add(value.value)
        unknown = found - set(WARNING_CODES)
        self.assertFalse(unknown, f"unregistered warning codes: {sorted(unknown)}")


class TestDynamicRegistryCoverage(unittest.TestCase):
    def assert_error_channel(self, argv: tuple[str, ...], expected_code: str) -> None:
        code, out, err = run_cli(*argv)
        self.assertNotEqual(code, 0, argv)
        env = json.loads(out)
        self.assertFalse(env["ok"], argv)
        observed = env["errors"][0]["code"]
        self.assertEqual(observed, expected_code, argv)
        self.assertIn(observed, ERROR_CODES, argv)
        self.assertIn("errors", CODE_REGISTRY[observed]["appears_in"], argv)
        self.assertNotIn("Traceback", err, argv)

    def assert_warning_channel(self, env: dict) -> None:
        self.assertTrue(env["warnings"])
        for warning in env["warnings"]:
            code = warning["code"]
            self.assertIn(code, WARNING_CODES)
            self.assertIn("warnings", CODE_REGISTRY[code]["appears_in"])

    def test_representative_error_codes_are_channel_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "queue.db")
            self.assert_error_channel(("statu", "--json"), "UNKNOWN_COMMAND")
            self.assert_error_channel(("status", "--json", "--bogus"), "UNKNOWN_FLAG")
            self.assert_error_channel(("--json",), "MISSING_REQUIRED")
            self.assert_error_channel(("show", "abc", "--db", db, "--json"), "INVALID_INPUT")
            self.assert_error_channel(("show", "999", "--db", db, "--json"), "NOT_FOUND")

            job_code, add_out, _ = run_cli(
                "add", "--db", db, "--json", "--", "sleep", "30"
            )
            self.assertEqual(job_code, 0)
            job_id = str(json.loads(add_out)["data"]["job_id"])
            conn = store.connect(db)
            try:
                store.claim_next(conn, "worker", 123, time.time(), store.output_root(db))
            finally:
                conn.close()
            self.assert_error_channel(
                ("retry", job_id, "--db", db, "--json"),
                "SAFETY_BLOCK",
            )

            self.assert_error_channel(
                ("wait", job_id, "--db", db, "--json", "--timeout", "0.01"),
                "TIMEOUT",
            )

            blocker = store.connect(db)
            self.addCleanup(blocker.close)
            blocker.execute("BEGIN IMMEDIATE")
            self.addCleanup(lambda: blocker.execute("ROLLBACK"))
            with mock.patch.object(store, "BUSY_TIMEOUT_MS", 1):
                self.assert_error_channel(
                    ("add", "--db", db, "--json", "--", "true"),
                    "LOCKED",
                )

    def test_representative_warning_codes_are_channel_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "queue.db")
            code, out, _ = run_cli("add", "--db", db, "--json", "--", "true")
            self.assertEqual(code, 0)
            job_id = str(json.loads(out)["data"]["job_id"])

            code, out, _ = run_cli("output", job_id, "--db", db, "--json")
            self.assertEqual(code, 0)
            self.assert_warning_channel(json.loads(out))

            conn = store.connect(db)
            try:
                store.claim_next(conn, "worker", 123, time.time(), store.output_root(db))
            finally:
                conn.close()
            code, out, _ = run_cli("cancel", job_id, "--db", db, "--json", "--running")
            self.assertEqual(code, 0)
            self.assert_warning_channel(json.loads(out))


if __name__ == "__main__":
    unittest.main()
