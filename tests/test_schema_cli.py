"""schema verb: db-free export of the machine-contract JSON Schemas."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from spoolctl import cli, schemas

GOLDEN = Path(__file__).resolve().parent / "golden" / "schema.json"


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestSchemaVerb(unittest.TestCase):
    maxDiff = None

    def schema_data(self, *extra: str) -> dict:
        code, out, err = run_cli("schema", "--json", *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)["data"]

    def test_json_exports_all_schema_sections(self):
        data = self.schema_data()
        self.assertEqual(data["dialect"], schemas.DIALECT)
        self.assertEqual(data["envelope_schema"], schemas.ENVELOPE_SCHEMA)
        self.assertEqual(set(data["verbs"]), set(schemas.VERB_SCHEMAS))
        self.assertEqual(data["streams"], schemas.STREAM_SCHEMAS)

    def test_verb_filter_keeps_envelope_and_streams(self):
        data = self.schema_data("--verb", "events")
        self.assertEqual(set(data["verbs"]), {"events"})
        self.assertEqual(data["verbs"]["events"], schemas.VERB_SCHEMAS["events"])
        self.assertEqual(data["envelope_schema"], schemas.ENVELOPE_SCHEMA)
        self.assertEqual(data["streams"], schemas.STREAM_SCHEMAS)

    def test_unknown_verb_fails_with_suggestion(self):
        code, out, _ = run_cli("schema", "--json", "--verb", "statu")
        self.assertEqual(code, 1)
        err = json.loads(out)["errors"][0]
        self.assertEqual(err["code"], "INVALID_INPUT")
        self.assertEqual(err["did_you_mean"], "status")

    def test_human_mode_lists_verbs_and_json_hint(self):
        code, out, err = run_cli("schema")
        self.assertEqual(code, 0, err)
        for verb in schemas.VERB_SCHEMAS:
            self.assertIn(verb, out)
        self.assertIn("run with --json", out)

    def test_works_without_existing_database_and_ignores_db_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "missing", "queue.db")
            data = self.schema_data("--db", db)
            self.assertEqual(data["dialect"], schemas.DIALECT)
            self.assertFalse(os.path.exists(db))
            self.assertFalse(os.path.exists(os.path.dirname(db)))

    def test_capabilities_marks_db_ignored(self):
        code, out, err = run_cli("capabilities", "--json")
        self.assertEqual(code, 0, err)
        schema = json.loads(out)["data"]["verbs"]["schema"]
        self.assertEqual(schema["ignores"], ["--db"])

    def test_schema_matches_golden(self):
        data = self.schema_data()
        got = json.dumps(data, indent=2, sort_keys=True) + "\n"
        self.assertEqual(got, GOLDEN.read_text())


if __name__ == "__main__":
    unittest.main()
