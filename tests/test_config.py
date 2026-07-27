"""Project config discovery, DB path precedence, and operation base_dir plumbing."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from spoolctl import cli, store
from spoolctl.config import (
    CONFIG_RELPATH,
    PRECEDENCE,
    resolve_effective_config,
    validate_config_file,
)
from spoolctl.contract import DB_VERBS
from spoolctl.errors import CliError
from spoolctl.operations import (
    AddInput,
    ConfigShowInput,
    ConfigValidateInput,
    OutputInput,
    StatusInput,
    WaitInput,
    config_show_operation,
    config_validate_operation,
    status_operation,
)


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@contextmanager
def chdir(path: str):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.config_dir = self.project / ".spoolctl"
        self.config_dir.mkdir()

    def write_config(self, data: dict) -> Path:
        path = self.config_dir / "config.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


class TestEffectiveConfig(ConfigTestCase):
    def test_absent_config_is_valid_and_uses_default(self):
        eff = resolve_effective_config(base_dir=str(self.project))
        self.assertEqual(eff.db_source, "default")
        self.assertEqual(
            eff.db_path,
            os.path.realpath(self.project / ".spoolctl" / "queue.db"),
        )
        self.assertEqual(eff.config_path, os.path.realpath(self.project / CONFIG_RELPATH))
        self.assertFalse(eff.config_exists)
        self.assertTrue(eff.config_valid)
        self.assertEqual(eff.as_dict()["precedence"], list(PRECEDENCE))

    def test_flag_overrides_environment_and_config(self):
        self.write_config({"version": 1, "db_path": "config.db"})
        flag = str(self.project / "flag.db")
        env = str(self.project / "env.db")
        with mock.patch.dict(os.environ, {"SPOOLCTL_DB": env}):
            eff = resolve_effective_config(flag, base_dir=str(self.project))
        self.assertEqual(eff.db_source, "flag")
        self.assertEqual(eff.db_path, os.path.realpath(flag))

    def test_environment_overrides_config(self):
        self.write_config({"version": 1, "db_path": "config.db"})
        env = str(self.project / "env.db")
        with mock.patch.dict(os.environ, {"SPOOLCTL_DB": env}):
            eff = resolve_effective_config(base_dir=str(self.project))
        self.assertEqual(eff.db_source, "environment")
        self.assertEqual(eff.db_path, os.path.realpath(env))

    def test_relative_config_db_path_resolves_relative_to_config_parent(self):
        self.write_config({"version": 1, "db_path": "queue.db"})
        eff = resolve_effective_config(base_dir=str(self.project))
        self.assertEqual(eff.db_source, "project_config")
        self.assertEqual(eff.db_path, os.path.realpath(self.config_dir / "queue.db"))

    def test_unknown_keys_are_ignored_by_runtime_resolver(self):
        self.write_config({"version": 1, "db_path": "queue.db", "future": True})
        eff = resolve_effective_config(base_dir=str(self.project))
        self.assertEqual(eff.db_source, "project_config")
        self.assertEqual(eff.ignored_keys, ["future"])

    def test_validate_config_is_strict_about_unknown_keys(self):
        path = self.write_config({"version": 1, "db_path": "queue.db", "future": True})
        with self.assertRaises(CliError) as raised:
            validate_config_file(str(path))
        self.assertEqual(raised.exception.code, "INVALID_INPUT")
        self.assertIn("future", raised.exception.message)

    def test_invalid_config_shapes_return_invalid_input(self):
        cases = [
            ("{", "malformed JSON"),
            ("[]", "JSON object"),
            (json.dumps({"version": 2}), "version"),
            (json.dumps({"version": True}), "version"),
            (json.dumps({"version": 1, "db_path": ""}), "must not be empty"),
            (json.dumps({"version": 1, "db_path": "x" * 4097}), "4096"),
        ]
        for body, message in cases:
            with self.subTest(message=message):
                path = self.config_dir / "config.json"
                path.write_text(body, encoding="utf-8")
                with self.assertRaises(CliError) as raised:
                    resolve_effective_config(base_dir=str(self.project))
                self.assertEqual(raised.exception.code, "INVALID_INPUT")
                self.assertIn(message, raised.exception.message)

    def test_base_dir_none_does_not_read_cwd_config(self):
        self.write_config({"version": 1, "db_path": "configured.db"})
        with chdir(str(self.project)):
            no_base = store.resolve_db_path(None, base_dir=None)
            with_base = store.resolve_db_path(None, base_dir=str(self.project))
        self.assertEqual(no_base, os.path.realpath(self.project / ".spoolctl" / "queue.db"))
        self.assertEqual(with_base, os.path.realpath(self.config_dir / "configured.db"))

    def test_symlinked_config_path_is_realpath_normalized(self):
        real = self.project / "real"
        real.mkdir()
        link = self.project / "link"
        link.symlink_to(real)
        self.write_config({"version": 1, "db_path": str(link / "queue.db")})
        eff = resolve_effective_config(base_dir=str(self.project))
        self.assertEqual(eff.db_path, os.path.realpath(real / "queue.db"))


class TestOperationBaseDir(ConfigTestCase):
    def test_operation_uses_explicit_base_dir_not_process_cwd(self):
        self.write_config({"version": 1, "db_path": "configured.db"})
        configured_db = str(self.config_dir / "configured.db")
        conn = store.connect(configured_db)
        try:
            store.add_job(conn, ["true"], 300, 3, 10.0)
        finally:
            conn.close()

        other = Path(self.tmp.name) / "other"
        other.mkdir()
        with chdir(str(other)):
            data = status_operation(
                StatusInput(db_path=None, limit=10, base_dir=str(self.project))
            )

        self.assertEqual(data["counts"]["queued"], 1)
        self.assertFalse((other / ".spoolctl" / "queue.db").exists())

    def test_operation_inputs_require_keyword_only_base_dir(self):
        with self.assertRaises(TypeError):
            StatusInput(db_path=None, limit=10)
        with self.assertRaises(TypeError):
            OutputInput(None, 1, None, "stdout")
        with self.assertRaises(TypeError):
            WaitInput(None, [1], None, 0.01)
        with self.assertRaises(TypeError):
            AddInput(
                db_path=None,
                argv=["true"],
                timeout=300,
                max_retries=3,
                max_crashes=None,
                now=10.0,
                key=None,
                tags={},
                note=None,
                priority=0,
                queue="default",
                next_run_at=None,
                cwd=None,
                env={},
            )

    def test_config_operations_require_keyword_only_base_dir(self):
        with self.assertRaises(TypeError):
            ConfigShowInput(None)
        with self.assertRaises(TypeError):
            ConfigValidateInput(None)


class TestConfigCli(ConfigTestCase):
    def run_in_project(self, *argv: str) -> tuple[int, str, str]:
        with chdir(str(self.project)):
            return run_cli(*argv)

    def test_config_show_default_json_and_human_do_not_open_database(self):
        code, out, err = self.run_in_project("config-show", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        self.assertEqual(data["sources"]["db_path"], "default")
        self.assertEqual(
            data["values"]["db_path"],
            os.path.realpath(self.project / ".spoolctl" / "queue.db"),
        )
        self.assertFalse((self.project / ".spoolctl" / "queue.db").exists())

        code, out, err = self.run_in_project("config-show")
        self.assertEqual(code, 0, err)
        self.assertIn("config_path:", out)
        self.assertIn("db_source: default", out)
        self.assertFalse((self.project / ".spoolctl" / "queue.db").exists())

    def test_config_show_reports_flag_env_config_and_ignored_keys(self):
        path = self.write_config({"version": 1, "db_path": "config.db", "future": True})
        flag_db = self.project / "flag.db"
        env_db = self.project / "env.db"

        data = config_show_operation(
            ConfigShowInput(db_path=None, base_dir=str(self.project))
        )
        self.assertEqual(data["config_path"], os.path.realpath(path))
        self.assertEqual(data["sources"]["db_path"], "project_config")
        self.assertEqual(data["ignored_keys"], ["future"])

        with mock.patch.dict(os.environ, {"SPOOLCTL_DB": str(env_db)}):
            code, out, err = self.run_in_project("config-show", "--json")
        self.assertEqual(code, 0, err)
        env_data = json.loads(out)["data"]
        self.assertEqual(env_data["sources"]["db_path"], "environment")
        self.assertEqual(env_data["values"]["db_path"], os.path.realpath(env_db))

        code, out, err = self.run_in_project(
            "config-show", "--db", str(flag_db), "--json"
        )
        self.assertEqual(code, 0, err)
        flag_data = json.loads(out)["data"]
        self.assertEqual(flag_data["sources"]["db_path"], "flag")
        self.assertEqual(flag_data["values"]["db_path"], os.path.realpath(flag_db))
        self.assertEqual(flag_data["ignored_keys"], ["future"])
        self.assertEqual(json.loads(out)["warnings"], [])
        self.assertFalse(flag_db.exists(), "config-show must not open --db path")

    def test_config_show_rejects_positional_and_bad_db_syntax(self):
        code, out, _ = self.run_in_project("config-show", "extra", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")

        code, out, _ = self.run_in_project("config-show", "--db", "", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")

    def test_config_validate_absent_default_is_valid(self):
        code, out, err = self.run_in_project("config-validate", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)["data"]
        self.assertEqual(data["config_path"], os.path.realpath(self.config_dir / "config.json"))
        self.assertFalse(data["exists"])
        self.assertTrue(data["valid"])
        self.assertEqual(data["format"], "json")
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["recognized_keys"], ["db_path"])
        self.assertEqual(data["unknown_keys"], [])

    def test_config_validate_explicit_path_ignores_default_config(self):
        (self.config_dir / "config.json").write_text("{", encoding="utf-8")
        explicit = self.project / "explicit.json"
        explicit.write_text(json.dumps({"version": 1, "db_path": "queue.db"}), encoding="utf-8")

        data = config_validate_operation(
            ConfigValidateInput(path=str(explicit), base_dir=str(self.project))
        )
        self.assertTrue(data["exists"])

        code, out, err = self.run_in_project("config-validate", str(explicit), "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["data"]["config_path"], os.path.realpath(explicit))

    def test_config_validate_unknown_key_fails_and_db_flag_is_unknown(self):
        self.write_config({"version": 1, "future": True})
        code, out, _ = self.run_in_project("config-validate", "--json")
        self.assertEqual(code, 1)
        error = json.loads(out)["errors"][0]
        self.assertEqual(error["code"], "INVALID_INPUT")
        self.assertIn("future", error["message"])

        code, out, _ = self.run_in_project("config-validate", "--db", "x", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "UNKNOWN_FLAG")


class TestCliDbVerbConfigDiscovery(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.write_config({"version": 1, "db_path": "configured.db"})
        self.configured_db = str(self.config_dir / "configured.db")
        self.default_db = self.config_dir / "queue.db"

    def run_in_project(self, *argv: str) -> tuple[int, str, str]:
        with chdir(str(self.project)):
            return run_cli(*argv)

    def reset_configured_database(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.configured_db + suffix)
            except FileNotFoundError:
                pass
        shutil.rmtree(self.config_dir / "output", ignore_errors=True)
        try:
            os.unlink(self.default_db)
        except FileNotFoundError:
            pass

    def setup_for(self, verb: str) -> None:
        if verb in {"events", "list", "prune", "status"}:
            conn = store.connect(self.configured_db)
            conn.close()
        elif verb == "add":
            pass
        elif verb == "cancel":
            run_cli("add", "--db", self.configured_db, "--json", "--", "sleep", "1")
        elif verb == "output":
            run_cli("add", "--db", self.configured_db, "--json", "--", "sh", "-c", "printf hi")
            run_cli("work", "--db", self.configured_db, "--json", "--once")
        elif verb == "retry":
            run_cli(
                "add", "--db", self.configured_db, "--json",
                "--max-retries", "0", "--", "false",
            )
            run_cli("work", "--db", self.configured_db, "--json", "--once")
        elif verb == "show":
            run_cli("add", "--db", self.configured_db, "--json", "--", "true")
        elif verb == "wait":
            run_cli("add", "--db", self.configured_db, "--json", "--", "true")
            run_cli("work", "--db", self.configured_db, "--json", "--once")
        elif verb == "work":
            run_cli("add", "--db", self.configured_db, "--json", "--", "true")
        else:
            raise AssertionError(f"unhandled DB verb setup: {verb}")

    def invocation_for(self, verb: str) -> list[str]:
        table = {
            "add": ["add", "--json", "--", "true"],
            "cancel": ["cancel", "1", "--json"],
            "events": ["events", "--json"],
            "list": ["list", "--json"],
            "output": ["output", "1", "--json"],
            "prune": ["prune", "--older-than", "1s", "--dry-run", "--json"],
            "retry": ["retry", "1", "--json"],
            "show": ["show", "1", "--json"],
            "status": ["status", "--json"],
            "wait": ["wait", "1", "--json"],
            "work": ["work", "--once", "--json"],
        }
        return table[verb]

    def test_every_db_verb_uses_config_supplied_database_without_db_flag(self):
        self.assertEqual(
            set(DB_VERBS),
            set(self.invocation_for(verb)[0] for verb in DB_VERBS),
            "update config-discovery coverage when DB_VERBS changes",
        )
        for verb in DB_VERBS:
            with self.subTest(verb=verb):
                self.reset_configured_database()
                self.setup_for(verb)
                code, out, err = self.run_in_project(*self.invocation_for(verb))
                self.assertIn(code, {0, 6}, (verb, out, err))
                self.assertTrue(Path(self.configured_db).exists(), verb)
                self.assertFalse(self.default_db.exists(), f"{verb} used default DB path")


if __name__ == "__main__":
    unittest.main()
