"""Doctor operation readiness checks and non-mutating SQLite inspection."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from spoolctl import cli, store
from spoolctl.errors import CliError
from spoolctl.models import SCHEMA_VERSION
from spoolctl.operations import DoctorInput, doctor_operation


def parser_verbs():
    cli.build_parser()
    return dict(cli._SUBPARSERS)


def check_map(data: dict) -> dict[str, dict]:
    return {check["id"]: check for check in data["checks"]}


def sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class DoctorTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "queue.db")

    def doctor(self, db_path: str | None = None, *, base_dir: str | None = None, verbs=None):
        return doctor_operation(
            DoctorInput(
                db_path=db_path,
                base_dir=base_dir,
                parser_verbs=parser_verbs() if verbs is None else verbs,
            )
        )


class TestDoctorReadiness(DoctorTestCase):
    def test_existing_clean_queue_is_ready(self):
        store.connect(self.db).close()

        data = self.doctor(self.db)

        self.assertTrue(data["ready"])
        self.assertEqual(data["summary"]["failed"], 0)
        checks = check_map(data)
        for check_id in (
            "config_valid",
            "db_path_resolved",
            "spool_directory_writable",
            "database_exists",
            "sqlite_open_readwrite",
            "schema_version",
            "contract_metadata",
        ):
            self.assertEqual(checks[check_id]["status"], "pass", check_id)
        self.assertEqual(data["config"]["db_path"], os.path.realpath(self.db))
        self.assertEqual(data["config"]["db_source"], "flag")
        self.assertEqual(data["versions"]["schema_version"], SCHEMA_VERSION)

    def test_missing_database_is_unready_and_dependent_checks_skip(self):
        data = self.doctor(self.db)

        self.assertFalse(data["ready"])
        checks = check_map(data)
        self.assertEqual(checks["database_exists"]["status"], "fail")
        self.assertIn("does not exist", checks["database_exists"]["message"])
        self.assertEqual(checks["sqlite_open_readwrite"]["status"], "skip")
        self.assertEqual(checks["sqlite_open_readwrite"]["blocked_by"], "database_exists")
        self.assertEqual(checks["schema_version"]["status"], "skip")
        self.assertEqual(data["summary"]["failed"], 1)
        self.assertFalse(os.path.exists(self.db), "doctor must not create missing DB")

    def test_missing_parent_is_readiness_failure_not_side_effect(self):
        missing_parent_db = str(self.root / "missing" / "queue.db")
        data = self.doctor(missing_parent_db)

        self.assertFalse(data["ready"])
        checks = check_map(data)
        self.assertEqual(checks["spool_directory_writable"]["status"], "fail")
        self.assertEqual(checks["database_exists"]["status"], "fail")
        self.assertFalse((self.root / "missing").exists())

    def test_existing_database_bytes_do_not_change(self):
        store.connect(self.db).close()
        before = sha256(self.db)

        data = self.doctor(self.db)

        self.assertTrue(data["ready"])
        self.assertEqual(sha256(self.db), before)

    def test_old_and_new_schema_are_unready_without_migration(self):
        for version, remediation in (
            (SCHEMA_VERSION - 1, "migrate"),
            (SCHEMA_VERSION + 1, "upgrade spoolctl"),
        ):
            with self.subTest(version=version):
                store.connect(self.db).close()
                conn = store.connect(self.db)
                conn.execute(
                    "UPDATE meta SET value=? WHERE key='schema_version'",
                    (str(version),),
                )
                conn.close()
                before = sha256(self.db)

                data = self.doctor(self.db)

                checks = check_map(data)
                self.assertFalse(data["ready"])
                self.assertEqual(checks["schema_version"]["status"], "fail")
                self.assertIn(remediation, checks["schema_version"]["remediation"])
                self.assertEqual(sha256(self.db), before)
                os.unlink(self.db)

    def test_malformed_config_is_structured_readiness_failure(self):
        config_dir = self.root / "project" / ".spoolctl"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text("{", encoding="utf-8")

        data = self.doctor(None, base_dir=str(self.root / "project"))

        self.assertFalse(data["ready"])
        checks = check_map(data)
        self.assertEqual(checks["config_valid"]["status"], "fail")
        self.assertEqual(checks["db_path_resolved"]["status"], "skip")
        self.assertEqual(data["config"]["config_valid"], False)

    def test_syntactically_invalid_db_flag_raises_input_error(self):
        with self.assertRaises(CliError) as raised:
            self.doctor("")
        self.assertEqual(raised.exception.code, "INVALID_INPUT")

    def test_unopenable_path_is_readiness_failure(self):
        parent_file = self.root / "parent-file"
        parent_file.write_text("not a directory", encoding="utf-8")

        data = self.doctor(str(parent_file / "queue.db"))

        self.assertFalse(data["ready"])
        self.assertGreaterEqual(data["summary"]["failed"], 1)
        self.assertEqual(check_map(data)["spool_directory_writable"]["status"], "fail")

    def test_contract_metadata_requires_adapter_parser_metadata(self):
        store.connect(self.db).close()
        data = self.doctor(self.db, verbs={})

        self.assertFalse(data["ready"])
        check = check_map(data)["contract_metadata"]
        self.assertEqual(check["status"], "fail")
        self.assertIn("parser metadata", check["message"])

    def test_doctor_input_requires_parser_verbs_keyword(self):
        with self.assertRaises(TypeError):
            DoctorInput(db_path=self.db, base_dir=None)

    def test_result_is_json_serializable(self):
        store.connect(self.db).close()
        json.dumps(self.doctor(self.db), sort_keys=True)


if __name__ == "__main__":
    unittest.main()
