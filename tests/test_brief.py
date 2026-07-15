"""brief verb: compact db-free usage reference for agents."""

from __future__ import annotations

import io
import json
import math
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from spoolctl import cli

GOLDEN = Path(__file__).resolve().parent / "golden" / "brief.txt"


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestBrief(unittest.TestCase):
    maxDiff = None

    def brief_data(self, *extra: str) -> dict:
        code, out, err = run_cli("brief", "--json", *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)["data"]

    def test_json_shape_budget_and_token_count(self):
        data = self.brief_data()
        text = data["text"]
        self.assertEqual(data["budget_tokens"], 700)
        self.assertEqual(data["approx_tokens"], math.ceil(len(text) / 4))
        self.assertLessEqual(len(text), 2800)
        self.assertTrue(text.endswith(f"~{data['approx_tokens']} tokens (budget 700)."))

    def test_human_mode_prints_text_verbatim(self):
        data = self.brief_data()
        code, out, err = run_cli("brief")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, data["text"] + "\n")

    def test_works_without_existing_database_and_ignores_db_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "missing", "queue.db")
            data = self.brief_data("--db", db)
            self.assertIn("spoolctl quick brief", data["text"])
            self.assertFalse(os.path.exists(db))
            self.assertFalse(os.path.exists(os.path.dirname(db)))

    def test_mentions_every_verb_and_required_operational_facts(self):
        text = self.brief_data()["text"]
        for verb in cli.VERBS:
            self.assertIn(verb, text)
        for required in [
            "spoolctl add -- <cmd>",
            "spoolctl work --drain",
            "spoolctl wait <ids>",
            "spoolctl output <id>",
            "Submit many jobs first",
            "wait exits 6",
            "data.all_succeeded=false",
            "Exit codes:",
            "SPOOLCTL_DB",
        ]:
            self.assertIn(required, text)

    def test_capabilities_marks_db_ignored(self):
        code, out, err = run_cli("capabilities", "--json")
        self.assertEqual(code, 0, err)
        brief = json.loads(out)["data"]["verbs"]["brief"]
        self.assertEqual(brief["ignores"], ["--db"])

    def test_text_matches_golden(self):
        text = self.brief_data()["text"]
        self.assertEqual(text + "\n", GOLDEN.read_text())


if __name__ == "__main__":
    unittest.main()
