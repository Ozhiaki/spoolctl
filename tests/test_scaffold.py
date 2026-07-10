"""Scaffold guarantees: stdlib-only imports, entry points, single-file build."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "spoolctl"


def _imported_top_level_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


class TestStdlibOnly(unittest.TestCase):
    def test_no_third_party_imports_anywhere(self):
        allowed = set(sys.stdlib_module_names) | {"spoolctl"}
        offenders = {}
        for path in sorted(list(PACKAGE.rglob("*.py")) + list((REPO / "tests").rglob("*.py"))):
            bad = _imported_top_level_names(path) - allowed
            if bad:
                offenders[str(path.relative_to(REPO))] = sorted(bad)
        self.assertEqual(offenders, {}, f"non-stdlib imports found: {offenders}")


class TestEntryPoints(unittest.TestCase):
    def test_python_dash_m_help(self):
        proc = subprocess.run(
            [sys.executable, "-m", "spoolctl", "--help"],
            cwd=REPO, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("spoolctl", proc.stdout)
        self.assertIn("capabilities", proc.stdout)

    def test_help_stays_terse(self):
        proc = subprocess.run(
            [sys.executable, "-m", "spoolctl", "--help"],
            cwd=REPO, capture_output=True, text=True,
        )
        core = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertLess(len(core), 30, "--help core must stay under 30 non-blank lines")


class TestSingleFileBuild(unittest.TestCase):
    def test_build_emits_runnable_single_file(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td) / "spoolctl.py"
            built = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "build_single_file.py"), str(artifact)],
                capture_output=True, text=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertTrue(artifact.exists())
            # Run from an empty cwd so nothing resolves against the repo checkout.
            proc = subprocess.run(
                [sys.executable, str(artifact), "--help"],
                cwd=td, capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("spoolctl", proc.stdout)
            # One add/work/wait round-trip through the artifact.
            db = str(Path(td) / "queue.db")
            def art(verb, *argv):
                p = subprocess.run(
                    [sys.executable, str(artifact), verb, "--db", db, "--json",
                     *argv],
                    cwd=td, capture_output=True, text=True,
                )
                return p.returncode, p.stdout, p.stderr
            code, out, err = art("add", "--", "echo", "roundtrip")
            self.assertEqual(code, 0, err)
            job_id = json.loads(out)["data"]["job_id"]
            code, out, err = art("work", "--once")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["data"]["result"], "succeeded")
            code, out, err = art("wait", str(job_id))
            self.assertEqual(code, 0, err)
            self.assertTrue(json.loads(out)["data"]["all_succeeded"])


if __name__ == "__main__":
    unittest.main()
