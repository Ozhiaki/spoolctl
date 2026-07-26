"""robot-docs guide: agent handbook output and paste-ready examples."""

from __future__ import annotations

import io
import json
import shlex
import unittest
from contextlib import redirect_stderr, redirect_stdout

from spoolctl import cli


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestRobotDocs(unittest.TestCase):
    def test_guide_json_shape(self):
        code, out, err = run_cli("robot-docs", "guide", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        self.assertIn("spoolctl robot-docs guide", data["text"])
        self.assertGreater(data["approx_tokens"], 0)
        self.assertGreaterEqual(len(data["sections"]), 4)
        for section in data["sections"]:
            self.assertIsInstance(section["title"], str)
            self.assertTrue(section["bullets"])

    def test_human_mode_prints_guide_text(self):
        code, out, err = run_cli("robot-docs", "guide", "--json")
        self.assertEqual(code, 0, err)
        text = json.loads(out)["data"]["text"]
        code, out, err = run_cli("robot-docs", "guide")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, text + "\n")

    def test_documented_examples_parse(self):
        code, out, err = run_cli("robot-docs", "guide", "--json")
        self.assertEqual(code, 0, err)
        parser = cli.build_parser()
        for section in json.loads(out)["data"]["sections"]:
            for bullet in section["bullets"]:
                if bullet.startswith("spoolctl "):
                    with self.subTest(example=bullet):
                        parser.parse_args(shlex.split(bullet)[1:])

    def test_missing_subcommand_is_machine_readable(self):
        code, out, err = run_cli("robot-docs", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "MISSING_REQUIRED")
        self.assertIn("robot-docs", err)


if __name__ == "__main__":
    unittest.main()
