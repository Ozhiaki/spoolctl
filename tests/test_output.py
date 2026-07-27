"""output verb: raw byte fidelity, historical attempts, headers, warnings."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from spoolctl import cli, store
from spoolctl.operations import OutputInput, output_operation

REPO = Path(__file__).resolve().parent.parent


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class OutputTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)

    def run_job(self, argv: list[str]) -> int:
        job_id = store.add_job(self.conn, argv, 300, 3, time.time())
        code, out, err = run_cli("work", "--once", "--db", self.db, "--json")
        assert code == 0, err
        return job_id

    def make_captured_attempt(self, stdout: bytes, stderr: bytes) -> int:
        job_id = store.add_job(self.conn, ["capture"], 300, 3, time.time())
        out_root = store.output_root(self.db)
        _, attempt = store.claim_next(self.conn, "w1", 42, time.time(), out_root)
        for path, body in ((attempt.stdout_path, stdout), (attempt.stderr_path, stderr)):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            Path(path).write_bytes(body)
        store.record_success(self.conn, job_id, attempt.id, "w1", 42, time.time())
        return job_id


class TestDirectOutputOperation(OutputTestCase):
    def test_selects_stdout_stderr_and_both_without_cli(self):
        job_id = self.make_captured_attempt(b"out", b"err")

        stdout = output_operation(
            OutputInput(self.db, job_id, None, "stdout", base_dir=None)
        )
        stderr = output_operation(
            OutputInput(self.db, job_id, None, "stderr", base_dir=None)
        )
        both = output_operation(
            OutputInput(self.db, job_id, None, "both", base_dir=None)
        )

        self.assertEqual(stdout.stream_bytes, {"stdout": b"out"})
        self.assertEqual(stderr.stream_bytes, {"stderr": b"err"})
        self.assertEqual(both.stream_bytes, {"stdout": b"out", "stderr": b"err"})
        self.assertEqual(stdout.data["streams"]["stdout"]["preview"], "out")
        self.assertEqual(stderr.data["streams"]["stderr"]["preview"], "err")


class TestRawFidelity(OutputTestCase):
    def test_raw_round_trips_binary_exactly(self):
        # python -c, not printf: dash's printf (ubuntu sh) lacks \xHH escapes
        payload = (
            "import sys; sys.stdout.buffer.write("
            "b'\\x00\\x01\\xff\\x7f\\x80binary\\ndata')"
        )
        job_id = self.run_job([sys.executable, "-c", payload])
        proc = subprocess.run(
            [sys.executable, "-m", "spoolctl", "output", str(job_id),
             "--db", self.db, "--raw", "--stream", "stdout"],
            cwd=REPO, capture_output=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"\x00\x01\xff\x7f\x80binary\ndata")

    def test_raw_requires_single_stream(self):
        job_id = self.run_job(["echo", "hi"])
        code, out, err = run_cli("output", str(job_id), "--db", self.db, "--raw")
        self.assertEqual(code, 1)
        self.assertIn("--stream stdout", err)

    def test_raw_and_json_mutually_exclusive(self):
        job_id = self.run_job(["echo", "hi"])
        code, out, err = run_cli("output", str(job_id), "--db", self.db,
                                 "--raw", "--stream", "stdout", "--json")
        self.assertEqual(code, 1)
        env = json.loads(out)
        self.assertFalse(env["ok"])
        self.assertEqual(env["errors"][0]["code"], "INVALID_INPUT")
        self.assertIn("--raw and --json", err)

    def test_raw_failures_keep_stdout_clean(self):
        proc = subprocess.run(
            [sys.executable, "-m", "spoolctl", "output", "999",
             "--db", self.db, "--raw", "--stream", "stdout"],
            cwd=REPO, capture_output=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(b"no job with id 999", proc.stderr)


class TestDefaultAndJson(OutputTestCase):
    def test_headers_identify_job_attempt_stream(self):
        job_id = self.run_job(["sh", "-c", "echo out-line; echo err-line 1>&2"])
        code, out, _ = run_cli("output", str(job_id), "--db", self.db)
        self.assertEqual(code, 0)
        self.assertIn(f"=== job {job_id} attempt 1 stdout ===", out)
        self.assertIn(f"=== job {job_id} attempt 1 stderr ===", out)
        self.assertIn("out-line", out)
        self.assertIn("err-line", out)

    def test_stream_filter(self):
        job_id = self.run_job(["sh", "-c", "echo out-line; echo err-line 1>&2"])
        code, out, _ = run_cli("output", str(job_id), "--db", self.db,
                               "--stream", "stderr")
        self.assertIn("err-line", out)
        self.assertNotIn("out-line", out)

    def test_json_returns_paths_sizes_previews(self):
        job_id = self.run_job(["sh", "-c", "printf hello"])
        code, out, _ = run_cli("output", str(job_id), "--db", self.db, "--json")
        data = json.loads(out)["data"]
        self.assertEqual(data["attempt_no"], 1)
        self.assertEqual(data["attempts_total"], 1)
        so = data["streams"]["stdout"]
        self.assertEqual(so["preview"], "hello")
        self.assertEqual(so["size_bytes"], 5)
        self.assertFalse(so["preview_truncated"])
        self.assertTrue(Path(so["path"]).exists())

    def test_json_preview_replaces_undecodable_bytes(self):
        job_id = self.run_job(
            [sys.executable, "-c",
             "import sys; sys.stdout.buffer.write(bytes([255, 254]))"])
        _, out, _ = run_cli("output", str(job_id), "--db", self.db, "--json")
        preview = json.loads(out)["data"]["streams"]["stdout"]["preview"]
        self.assertEqual(preview, "��")


class TestAttemptsAndEdgeCases(OutputTestCase):
    def test_historical_attempt_after_retry(self):
        job_id = store.add_job(self.conn, ["sh", "-c", "printf run-one; exit 1"],
                               300, 0, time.time())
        run_cli("work", "--once", "--db", self.db, "--json")  # -> dead
        run_cli("retry", str(job_id), "--db", self.db, "--json")
        self.conn.execute("UPDATE jobs SET argv_json=? WHERE id=?",
                          ('["sh","-c","printf run-two"]', job_id))
        run_cli("work", "--once", "--db", self.db, "--json")
        # default = latest
        _, out, _ = run_cli("output", str(job_id), "--db", self.db,
                            "--stream", "stdout")
        self.assertIn("run-two", out)
        # historical attempt 1 still retrievable
        _, out, _ = run_cli("output", str(job_id), "--db", self.db,
                            "--attempt", "1", "--stream", "stdout")
        self.assertIn("run-one", out)

    def test_missing_attempt_number(self):
        job_id = self.run_job(["echo", "hi"])
        code, out, _ = run_cli("output", str(job_id), "--db", self.db,
                               "--attempt", "9", "--json")
        self.assertEqual(code, 1)
        e = json.loads(out)["errors"][0]
        self.assertEqual(e["code"], "NOT_FOUND")
        self.assertIn("available attempts: 1", e["remediation"])

    def test_no_attempts_yet_exit_zero_with_warning(self):
        job_id = store.add_job(self.conn, ["echo", "hi"], 300, 3, time.time())
        code, out, err = run_cli("output", str(job_id), "--db", self.db, "--json")
        self.assertEqual(code, 0)
        env = json.loads(out)
        self.assertTrue(env["ok"])
        self.assertEqual(env["data"], {"attempts": []})
        self.assertEqual(env["warnings"][0]["code"], "NO_ATTEMPTS_YET")

    def test_unknown_id_exit_1(self):
        code, out, _ = run_cli("output", "424242", "--db", self.db, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["errors"][0]["code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
