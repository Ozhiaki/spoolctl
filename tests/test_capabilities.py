"""capabilities verb: golden-pinned contract, parser parity, determinism."""

from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from spoolctl import cli
from spoolctl.models import CODE_REGISTRY, ERROR_CODES, FAILURE_REASONS, WARNING_CODES

GOLDEN = Path(__file__).resolve().parent / "golden" / "capabilities.json"


def capabilities_data() -> dict:
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = cli.main(["capabilities", "--json"])
    assert code == 0
    return json.loads(out.getvalue())["data"]


class TestGoldenPin(unittest.TestCase):
    def test_contract_matches_golden_file(self):
        got = json.dumps(capabilities_data(), indent=2, sort_keys=True) + "\n"
        want = GOLDEN.read_text()
        self.assertEqual(
            got, want,
            "\n\ncapabilities contract drifted from tests/golden/capabilities.json."
            "\nIf the change is intentional: re-pin the golden file and add a"
            " changelog entry.\n",
        )

    def test_deterministic_across_calls(self):
        self.assertEqual(capabilities_data(), capabilities_data())


class TestParserParity(unittest.TestCase):
    def test_every_parser_flag_in_capabilities_and_vice_versa(self):
        cli.build_parser()
        data = capabilities_data()
        for verb, sub in cli._SUBPARSERS.items():
            parser_flags = set()
            for action in sub._actions:
                if isinstance(action, argparse._HelpAction) or not action.option_strings:
                    continue
                longs = [s for s in action.option_strings if s.startswith("--")]
                parser_flags.add(longs[0] if longs else max(action.option_strings, key=len))
            caps_flags = {f["flag"] for f in data["verbs"][verb]["flags"]}
            self.assertEqual(parser_flags, caps_flags, f"flag drift in verb {verb!r}")

    def test_every_verb_present(self):
        data = capabilities_data()
        self.assertEqual(set(data["verbs"]), set(cli.VERBS))

    def test_exit_codes_cover_dictionary(self):
        data = capabilities_data()
        self.assertEqual(set(data["exit_codes"]), {"0", "1", "2", "3", "4", "5", "6"})
        self.assertIs(data["exit_codes"]["4"]["retryable"], True)
        self.assertIs(data["exit_codes"]["1"]["retryable"], False)

    def test_exit_6_documents_the_ok_true_exception(self):
        info = capabilities_data()["exit_codes"]["6"]
        self.assertEqual(
            info["meaning"], "job-outcome-failure (an awaited job ended non-success)"
        )
        self.assertIs(info["retryable"], False)
        self.assertIn("ok:true", info["note"])
        self.assertIn("data.all_succeeded", info["note"])

    def test_canceled_enumerated_and_policy_stated(self):
        data = capabilities_data()
        self.assertIn("canceled", data["job_states"])
        self.assertIn("canceled", data["attempt_states"])
        self.assertIn("canceled", data["events"])
        self.assertIn("additive", data["contract_policy"])
        self.assertIn("contract_version", data["contract_policy"])

    def test_failure_reasons_registry_documented(self):
        data = capabilities_data()
        self.assertEqual(data["failure_reasons"], list(FAILURE_REASONS))

    def test_code_registry_documented(self):
        data = capabilities_data()
        self.assertEqual(data["code_registry"], CODE_REGISTRY)
        self.assertEqual(set(data["error_codes"]), set(ERROR_CODES))
        warning_codes = {
            code for code, entry in data["code_registry"].items()
            if "warnings" in entry["appears_in"]
        }
        self.assertEqual(warning_codes, set(WARNING_CODES))

    def test_env_vars_documented(self):
        data = capabilities_data()
        self.assertEqual(
            set(data["env"]),
            {"SPOOLCTL_DB", "SPOOLCTL_TEST_HEARTBEAT_INTERVAL",
             "SPOOLCTL_TEST_REAP_THRESHOLD"},
        )
        self.assertEqual(set(data["env_vars"]), set(data["env"]))
        for name, entry in data["env_vars"].items():
            self.assertIn("type", entry, name)
            self.assertIn("malformed_expectations", entry, name)
            self.assertIn("consumed_by", entry, name)
            self.assertTrue(entry["consumed_by"], name)

    def test_events_declares_frames_follow_mode(self):
        events = capabilities_data()["verbs"]["events"]
        self.assertEqual(events["output_modes"], ["envelope", "frames", "text"])
        self.assertEqual(events["frames"]["enter_with"], ["--follow", "--json"])
        self.assertEqual(events["frames"]["record_schema"], "#/streams/events_follow")
        self.assertIn("integer id", events["frames"]["frame_discriminator"])
        self.assertEqual(events["frames"]["control_frames"], ["end", "error"])
        self.assertEqual(events["frames"]["cursor"]["flag"], "--since-id")
        self.assertEqual(events["frames"]["cursor"]["aliases"], ["--since-cursor"])
        self.assertEqual(events["since_cursor_alias"], "--since-cursor")

    def test_scheduling_contract_documented(self):
        scheduling = capabilities_data()["scheduling"]
        self.assertIn("<number>[s|m|h|d]", scheduling["duration_grammar"])
        self.assertEqual(scheduling["queue"]["default"], "default")
        self.assertIn("^[A-Za-z0-9]", scheduling["queue"]["grammar"])
        self.assertEqual(scheduling["priority"]["min"], -2147483648)
        self.assertEqual(scheduling["priority"]["max"], 2147483647)
        self.assertTrue(scheduling["slots"]["opt_in"])
        self.assertTrue(scheduling["slots"]["fleet_global"])
        self.assertIsNone(scheduling["slots"]["default_ceiling"])
        self.assertIn("claimed:false", scheduling["slots"]["claimed_false"])
        self.assertIn("retry/reap backoff rows", scheduling["scheduled"]["includes"])
        self.assertIn("attempts = 0", scheduling["drain"]["skips"])

    def test_execution_contract_documented(self):
        execution = capabilities_data()["execution"]
        self.assertEqual(execution["cwd"]["flag"], "--cwd DIR")
        self.assertIn("abspath", execution["cwd"]["resolution"])
        self.assertEqual(execution["env_overrides"]["flag"], "--env K=V")
        self.assertFalse(execution["env_overrides"]["values_in_add_or_list"])
        self.assertTrue(execution["env_overrides"]["values_in_show"])
        retry = execution["retry_model"]
        self.assertIn("attempts - crashes", retry["job_owned_failures"])
        self.assertEqual(retry["max_crashes"]["zero"], "first crash dead-letters")

    def test_safety_contract_documented(self):
        verbs = capabilities_data()["verbs"]
        prune = verbs["prune"]
        self.assertIs(prune["destructive"], True)
        self.assertEqual(prune["safety"]["confirmation_flag"], "--yes")
        self.assertEqual(prune["safety"]["dry_run_flag"], "--dry-run")
        self.assertEqual(prune["safety"]["refusal_code"], "SAFETY_BLOCK")

        cancel = verbs["cancel"]
        self.assertEqual(cancel["destructive"], "only with --running")
        self.assertEqual(cancel["interrupts_process"], "only with --running")
        self.assertEqual(cancel["safety"]["requires"], ["--running", "--yes"])
        self.assertFalse(cancel["safety"]["queued_cancel"]["destructive"])

        retry = verbs["retry"]
        self.assertEqual(retry["safety"]["force_required_for"], "running_job")
        self.assertFalse(retry["safety"]["also_requires_yes"])

    def test_make_cli_contract_shape_documented(self):
        data = capabilities_data()
        required = {
            "config",
            "env_vars",
            "features",
            "global_flags",
            "limits",
            "output_modes",
            "probe_vocabularies",
            "robot_docs_uri",
            "schemas_uri",
            "tool_name",
            "tool_version",
            "totality",
        }
        self.assertLessEqual(required, set(data))
        self.assertEqual(data["tool_name"], "spoolctl")
        self.assertIn("probeable_surface", data["features"])
        self.assertEqual(data["output_modes"], ["envelope", "frames", "raw", "text"])
        self.assertFalse(data["config"]["supported"])
        self.assertIn("--db is verb-local", data["global_flags"]["db_scope"])
        self.assertEqual(
            {f["name"] for f in data["global_flags"]["flags"]},
            {"--help", "--version"},
        )

    def test_every_verb_has_probeable_fields(self):
        data = capabilities_data()
        for name, verb in data["verbs"].items():
            with self.subTest(verb=name):
                self.assertIn("description", verb)
                self.assertIn("mutates", verb)
                self.assertIn("destructive", verb)
                self.assertIn("idempotent", verb)
                self.assertIn("json", verb)
                self.assertIn("output_modes", verb)
                self.assertIn("stdin", verb)
                self.assertIn("args", verb)
                self.assertIn("flags", verb)
                self.assertIn("mutually_exclusive", verb)
                self.assertIn("exit_codes", verb)
                self.assertIn("schema_ref", verb)
                self.assertIn("examples", verb)
                self.assertIn("probe_hints", verb)
                self.assertEqual(verb["stdin"], "none")
                self.assertIn("text", verb["output_modes"])
                self.assertIn("envelope", verb["output_modes"])
                for flag in verb["flags"]:
                    self.assertEqual(flag["name"], flag["flag"])
                    self.assertIn("value_required", flag)
                    self.assertIn("malformed_expectations", flag)
                    if flag["type"] in {"integer", "float", "duration", "timestamp"}:
                        self.assertIn("minimum", flag)
                        self.assertIn("maximum", flag)
                for arg in verb["args"]:
                    self.assertIn(arg["nargs"], {"1", "+", "*", "remainder"})
                    self.assertEqual(
                        arg["repeatable"],
                        arg["nargs"] in {"+", "*", "remainder"},
                    )
                    if arg["type"] == "integer":
                        self.assertIn("minimum", arg)
                        self.assertIn("maximum", arg)

    def test_length_constrained_strings_declare_rules(self):
        data = capabilities_data()
        for limit_name in [
            "cwd_length",
            "env_key_length",
            "env_value_length",
            "idempotency_key_length",
            "note_length",
            "queue_name",
            "tag_key_length",
            "tag_value_length",
            "worker_id_length",
        ]:
            entry = data["limits"][limit_name]
            self.assertIn("maximum", entry, limit_name)
            self.assertFalse(entry["unbounded"], limit_name)
        self.assertIn("charset", data["limits"]["queue_name"])
        self.assertIn("charset", data["limits"]["tag_key_length"])


if __name__ == "__main__":
    unittest.main()
