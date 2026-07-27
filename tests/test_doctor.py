"""Doctor operation readiness checks and non-mutating SQLite inspection."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from spoolctl import cli, store
from spoolctl.errors import CliError
from spoolctl.models import SCHEMA_VERSION
from spoolctl.operations import DoctorInput, doctor_operation


def parser_verbs():
    cli.build_parser()
    return dict(cli._SUBPARSERS)


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


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


class TestDoctorCli(DoctorTestCase):
    def test_doctor_json_ready_exit_zero(self):
        store.connect(self.db).close()

        code, out, err = run_cli("doctor", "--db", self.db, "--json")

        self.assertEqual(code, 0, err)
        env = json.loads(out)
        self.assertTrue(env["ok"])
        self.assertTrue(env["data"]["ready"])
        self.assertEqual(env["errors"], [])
        self.assertIn("checks", env["data"])
        self.assertIn("versions", env["data"])

    def test_doctor_json_unready_exit_three_but_ok_true(self):
        code, out, err = run_cli("doctor", "--db", self.db, "--json")

        self.assertEqual(code, 3, err)
        env = json.loads(out)
        self.assertTrue(env["ok"])
        self.assertEqual(env["errors"], [])
        self.assertFalse(env["data"]["ready"])
        checks = check_map(env["data"])
        self.assertEqual(checks["database_exists"]["status"], "fail")
        self.assertEqual(checks["schema_version"]["status"], "skip")

    def test_doctor_human_output_is_compact(self):
        code, out, err = run_cli("doctor", "--db", self.db)

        self.assertEqual(code, 3)
        self.assertIn("ready: no", out)
        self.assertIn("checks:", out)
        self.assertIn("failed checks:", out)
        self.assertIn("database_exists", out)
        self.assertNotIn("Traceback", out + err)

    def test_doctor_rejects_positional_and_syntactically_bad_db(self):
        code, out, _ = run_cli("doctor", "extra", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")

        code, out, _ = run_cli("doctor", "--db", "", "--json")
        self.assertEqual(code, 1)
        env = json.loads(out)
        self.assertFalse(env["ok"])
        self.assertEqual(env["errors"][0]["code"], "INVALID_INPUT")

    def test_doctor_malformed_config_is_readiness_result(self):
        project = self.root / "project"
        config_dir = project / ".spoolctl"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text("{", encoding="utf-8")
        old = os.getcwd()
        os.chdir(project)
        try:
            code, out, err = run_cli("doctor", "--json")
        finally:
            os.chdir(old)

        self.assertEqual(code, 3, err)
        env = json.loads(out)
        self.assertTrue(env["ok"])
        self.assertFalse(env["data"]["ready"])
        self.assertEqual(check_map(env["data"])["config_valid"]["status"], "fail")

    def test_doctor_unopenable_path_is_readiness_result(self):
        parent_file = self.root / "parent-file"
        parent_file.write_text("not a dir", encoding="utf-8")

        code, out, err = run_cli("doctor", "--db", str(parent_file / "queue.db"), "--json")

        self.assertEqual(code, 3, err)
        env = json.loads(out)
        self.assertTrue(env["ok"])
        self.assertFalse(env["data"]["ready"])
        self.assertEqual(
            check_map(env["data"])["spool_directory_writable"]["status"],
            "fail",
        )

    def test_doctor_cli_does_not_mutate_existing_database(self):
        store.connect(self.db).close()
        before = sha256(self.db)

        code, out, err = run_cli("doctor", "--db", self.db, "--json")

        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["data"]["ready"])
        self.assertEqual(sha256(self.db), before)


if __name__ == "__main__":
    unittest.main()
