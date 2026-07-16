"""capabilities verb: golden-pinned contract, parser parity, determinism."""

from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from spoolctl import cli
from spoolctl.models import FAILURE_REASONS

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
                parser_flags.add(max(action.option_strings, key=len))
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

    def test_env_vars_documented(self):
        data = capabilities_data()
        self.assertEqual(
            set(data["env"]),
            {"SPOOLCTL_DB", "SPOOLCTL_TEST_HEARTBEAT_INTERVAL",
             "SPOOLCTL_TEST_REAP_THRESHOLD"},
        )

    def test_events_declares_raw_follow_mode(self):
        events = capabilities_data()["verbs"]["events"]
        self.assertEqual(events["output_modes"], ["envelope", "raw"])
        self.assertEqual(events["raw"]["stream"], "events_follow")
        self.assertIn("no control frames", events["raw"]["record"])
        self.assertEqual(events["since_cursor_alias"], "--since-id")

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


if __name__ == "__main__":
    unittest.main()
