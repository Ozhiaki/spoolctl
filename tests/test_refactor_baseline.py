"""v0.4.6 refactor baseline guardrails."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "golden"


def import_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def add_at_line(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("schedule:add-at "):
            return line
    raise AssertionError(f"{path} has no schedule:add-at line")


class TestSignatureHarness(unittest.TestCase):
    def test_source_signature_covers_required_cells(self):
        with tempfile.TemporaryDirectory() as td:
            env = {**os.environ, "TZ": "UTC", "LC_ALL": "C"}
            proc = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "scripts" / "signature.py"),
                    "--target",
                    "source",
                    "--baseline",
                    str(Path(td) / "missing-baseline.txt"),
                ],
                cwd=REPO,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("meta:capabilities", proc.stdout)
        self.assertIn("meta:schema", proc.stdout)
        self.assertIn("meta:brief-text", proc.stdout)
        self.assertIn("meta:robot-docs", proc.stdout)
        self.assertIn("meta:config-show", proc.stdout)
        self.assertIn("meta:config-validate", proc.stdout)
        self.assertIn("invalid:unknown-verb", proc.stdout)
        self.assertIn("invalid:unknown-flag", proc.stdout)
        self.assertIn("invalid:missing-required", proc.stdout)
        self.assertIn("invalid:mutually-exclusive", proc.stdout)
        self.assertIn("invalid:safety-gate", proc.stdout)
        self.assertIn("schedule:add-at", proc.stdout)
        self.assertIn("schedule:add-after", proc.stdout)
        self.assertIn("roundtrip:output-json", proc.stdout)
        self.assertIn("roundtrip:doctor", proc.stdout)
        self.assertIn("text:output", proc.stdout)
        self.assertGreater(proc.stdout.count("detail=generated-invalid"), 20)
        self.assertNotIn("traceback=True", proc.stdout)
        self.assertNotIn("exit=HANG", proc.stdout)

    def test_signature_base_exists_for_every_verb(self):
        signature = import_script("signature", REPO / "scripts" / "signature.py")
        from spoolctl.models import VERBS

        missing = [verb for verb in VERBS if signature.base_for(verb, "/tmp/sig.db") is None]
        self.assertEqual(missing, [])

    def test_signature_path_normalization_prefers_realpaths(self):
        signature = import_script("signature", REPO / "scripts" / "signature.py")
        roots = signature.normalize_roots(Path("/tmp/spoolctl-x"), REPO)
        text = f"{os.path.realpath('/tmp/spoolctl-x')}/queue.db {REPO}/spoolctl"
        normalized = signature.normalize_paths(text, roots)
        self.assertNotIn(os.path.realpath("/tmp/spoolctl-x"), normalized)
        self.assertIn("<TMP>/queue.db", normalized)
        self.assertIn("<CWD>/spoolctl", normalized)
        signature.assert_no_stray_absolute_path("path-test", ["db=<TMP>/queue.db"])
        with self.assertRaises(SystemExit):
            signature.assert_no_stray_absolute_path("path-test", ["db=/tmp/spoolctl-x"])

    def test_checked_in_timezone_baselines_are_non_vacuous(self):
        for target in ("source", "single-file"):
            utc = GOLDEN / f"signature-{target}-UTC.txt"
            non_utc = GOLDEN / f"signature-{target}-XXX5.txt"
            self.assertTrue(utc.exists(), utc)
            self.assertTrue(non_utc.exists(), non_utc)
            self.assertNotEqual(add_at_line(utc), add_at_line(non_utc), target)


class TestRepeatedLiteralScan(unittest.TestCase):
    def test_scan_reports_extraction_sensitive_literals(self):
        repeated_literals = import_script(
            "repeated_literals", REPO / "scripts" / "repeated_literals.py"
        )
        found = repeated_literals.scan()
        self.assertIn("'canceled'", found)
        self.assertIn("'dead'", found)
        self.assertIn("'done'", found)
        self.assertIn("'^[A-Za-z0-9_.:-]+$'", found)
        self.assertIn("'^[A-Za-z0-9][A-Za-z0-9._-]*$'", found)


if __name__ == "__main__":
    unittest.main()
