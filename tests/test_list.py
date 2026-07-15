"""list verb: --state grammar, --limit grammar, ordering, determinism."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from spoolctl import cli, store


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ListTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def populate(self) -> None:
        """Jobs 1..4: done, dead, queued, running (in claim order)."""
        conn = store.connect(self.db)
        out_root = store.output_root(self.db)
        store.add_job(conn, ["echo", "one"], 300, 3, 10.0)
        store.add_job(conn, ["echo", "two"], 300, 0, 11.0)
        store.add_job(conn, ["echo", "three"], 300, 3, 12.0)
        store.add_job(conn, ["echo", "four"], 300, 3, 13.0)
        _, a1 = store.claim_next(conn, "w1", 42, 20.0, out_root)
        store.record_success(conn, 1, a1.id, "w1", 42, 21.0)
        _, a2 = store.claim_next(conn, "w1", 42, 22.0, out_root)
        store.record_failure(conn, 2, a2.id, "w1", 42, "failed", 1, "exit 1", 23.0)
        store.claim_next(conn, "w1", 42, 24.0, out_root)  # job 3 now running
        conn.close()

    def list_data(self, *extra: str) -> dict:
        code, out, err = run_cli("list", "--db", self.db, "--json", *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)["data"]

    def add_cli(self, *extra: str):
        code, out, err = run_cli("add", "--db", self.db, "--json", *extra, "--", "true")
        self.assertEqual(code, 0, err)
        return json.loads(out)["data"]["job_id"]


class TestFilterAndOrder(ListTestCase):
    def test_no_filter_returns_all_newest_first(self):
        self.populate()
        data = self.list_data()
        self.assertEqual(data["count"], 4)
        self.assertEqual([j["id"] for j in data["jobs"]], [4, 3, 2, 1])
        self.assertEqual(data["jobs"][3]["argv"], ["echo", "one"])
        self.assertEqual(data["jobs"][3]["idempotency_key"], None)
        self.assertEqual(data["jobs"][3]["tags"], {})
        self.assertEqual(data["jobs"][3]["note"], None)

    def test_single_state_filter(self):
        self.populate()
        data = self.list_data("--state", "queued")
        self.assertEqual([j["id"] for j in data["jobs"]], [4])
        self.assertEqual(data["jobs"][0]["state"], "queued")

    def test_csv_state_filter(self):
        self.populate()
        data = self.list_data("--state", "done,dead")
        self.assertEqual([j["id"] for j in data["jobs"]], [2, 1])

    def test_canceled_is_a_valid_filter_value(self):
        self.populate()
        data = self.list_data("--state", "canceled")
        self.assertEqual(data, {"count": 0, "jobs": []})

    def test_empty_queue_exits_zero_with_count_zero(self):
        data = self.list_data()
        self.assertEqual(data, {"count": 0, "jobs": []})


class TestStateGrammar(ListTestCase):
    def test_unknown_state_exits_1_with_did_you_mean(self):
        code, out, err = run_cli("list", "--db", self.db, "--json", "--state", "qeued")
        self.assertEqual(code, 1)
        errors = json.loads(out)["errors"]
        self.assertEqual(errors[0]["code"], "INVALID_INPUT")
        self.assertEqual(errors[0]["did_you_mean"], "queued")
        self.assertIn("queued", err)

    def test_unknown_state_without_near_miss_lists_valid_states(self):
        code, out, _ = run_cli("list", "--db", self.db, "--json", "--state", "bogus")
        self.assertEqual(code, 1)
        errors = json.loads(out)["errors"]
        self.assertIn("valid states", errors[0]["remediation"])

    def test_empty_state_token_rejected(self):
        code, _, _ = run_cli("list", "--db", self.db, "--json", "--state", "queued,")
        self.assertEqual(code, 1)

    def test_whitespace_around_tokens_tolerated(self):
        self.populate()
        data = self.list_data("--state", " done , dead ")
        self.assertEqual(data["count"], 2)


class TestLimitGrammar(ListTestCase):
    def test_limit_truncates_newest_first(self):
        self.populate()
        data = self.list_data("--limit", "2")
        self.assertEqual([j["id"] for j in data["jobs"]], [4, 3])

    def test_limit_zero_means_unlimited(self):
        self.populate()
        data = self.list_data("--limit", "0")
        self.assertEqual(data["count"], 4)

    def test_negative_limit_rejected(self):
        code, out, _ = run_cli("list", "--db", self.db, "--json", "--limit", "-1")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")


class TestTagFilter(ListTestCase):
    def test_tag_existence_and_exact_value_filters(self):
        self.add_cli("--tag", "owner=agent", "--tag", "mode=fast")
        self.add_cli("--tag", "owner=human")
        data = self.list_data("--tag", "owner")
        self.assertEqual([j["id"] for j in data["jobs"]], [2, 1])
        data = self.list_data("--tag", "owner=agent")
        self.assertEqual([j["id"] for j in data["jobs"]], [1])

    def test_repeated_tag_filters_are_and_ed_with_state(self):
        first = self.add_cli("--tag", "owner=agent", "--tag", "mode=fast")
        self.add_cli("--tag", "owner=agent", "--tag", "mode=slow")
        run_cli("cancel", str(first), "--db", self.db, "--json")
        data = self.list_data("--state", "canceled", "--tag", "owner=agent",
                              "--tag", "mode=fast")
        self.assertEqual([j["id"] for j in data["jobs"]], [1])

    def test_tag_limit_applies_after_filtering(self):
        for _ in range(5):
            self.add_cli()
        self.add_cli("--tag", "owner=agent")
        self.add_cli("--tag", "owner=agent")
        data = self.list_data("--tag", "owner=agent", "--limit", "2")
        self.assertEqual([j["id"] for j in data["jobs"]], [7, 6])

    def test_tag_keys_with_json_path_characters_filter_correctly(self):
        self.add_cli("--tag", "a.b=dot", "--tag", "a:b=colon", "--tag", "a-b=dash")
        for pred in ("a.b=dot", "a:b=colon", "a-b=dash"):
            data = self.list_data("--tag", pred)
            self.assertEqual([j["id"] for j in data["jobs"]], [1])

    def test_bad_list_tag_rejected(self):
        for raw in ("", "bad key", "x" * 129):
            with self.subTest(raw=raw):
                code, out, _ = run_cli("list", "--db", self.db, "--json",
                                       "--tag", raw)
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["errors"][0]["code"], "INVALID_INPUT")


class TestDeterminism(ListTestCase):
    def test_identical_calls_identical_data_hash(self):
        self.populate()
        _, out1, _ = run_cli("list", "--db", self.db, "--json")
        _, out2, _ = run_cli("list", "--db", self.db, "--json")
        h1 = json.loads(out1)["meta"]["data_hash"]
        h2 = json.loads(out2)["meta"]["data_hash"]
        self.assertEqual(h1, h2)


class TestHuman(ListTestCase):
    def test_one_line_per_job_and_truncation(self):
        self.populate()
        conn = store.connect(self.db)
        store.add_job(conn, ["echo", "x" * 200], 300, 3, 30.0)
        conn.close()
        code, out, _ = run_cli("list", "--db", self.db)
        self.assertEqual(code, 0)
        lines = out.rstrip("\n").split("\n")
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("#5  queued  attempts=0  "))
        command = lines[0].split("attempts=0  ", 1)[1]
        self.assertEqual(len(command), 80)
        self.assertTrue(command.endswith("..."))

    def test_empty_queue_human(self):
        code, out, _ = run_cli("list", "--db", self.db)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "No jobs")


if __name__ == "__main__":
    unittest.main()
