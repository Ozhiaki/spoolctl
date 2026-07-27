"""Documentation tests: drift gates, coverage gates, structural gates.

All tests are stdlib-only and must pass without mkdocs installed.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
PARENT_DIR = REPO.parent
MKDOCS_YML = REPO / "mkdocs.yml"

GENERATED_PAGES = [
    DOCS / "verbs.md",
    DOCS / "errors.md",
    DOCS / "reference" / "limits.md",
    DOCS / "reference" / "states.md",
]

_HARDCODED_PARENT_FILENAMES = {
    "ROADMAP.md", "RELEASE_UPDATES.md", "HANDOFF.md",
}

_FORWARD_VERSION_ALLOWLIST = {
    # install.md mentions future pip/uv paths
    "pip install spoolctl",
}


def _all_docs_md():
    """All .md files under docs/."""
    return sorted(DOCS.rglob("*.md"))


def _is_generated(path):
    return path in GENERATED_PAGES


def _parse_mkdocs_nav():
    """Narrow line-oriented parser for the nav: block in mkdocs.yml.

    Only handles the fixed indentation used in this project:
    - nav list items at 2-space indent: "  - Label: path.md"
    - section headers at 2-space indent: "  - Section Name:"
    - section items at 4-space indent: "    - Label: path.md"

    Fails closed on any line inside the nav block it cannot classify.
    """
    if not MKDOCS_YML.exists():
        return None

    lines = MKDOCS_YML.read_text().splitlines()
    in_nav = False
    nav_entries = []

    for lineno, line in enumerate(lines, 1):
        if not in_nav:
            if line.rstrip() == "nav:":
                in_nav = True
            continue

        if not line.strip():
            continue

        if not line.startswith(" "):
            break

        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if not stripped.startswith("- "):
            raise ValueError(
                f"unsupported mkdocs.yml nav shape: line {lineno}: {line!r}"
            )

        item = stripped[2:].strip()

        if ":" not in item:
            raise ValueError(
                f"unsupported mkdocs.yml nav shape: line {lineno}: {line!r}"
            )

        key, _, val = item.partition(":")
        val = val.strip()

        if indent not in (2, 4, 6):
            raise ValueError(
                f"unsupported mkdocs.yml nav shape: line {lineno}: {line!r}"
            )

        if val:
            nav_entries.append(val)

    return nav_entries


def _extract_md_links(content):
    """Extract relative .md links from Markdown content."""
    links = []
    for m in re.finditer(r'\[.*?\]\(([^)]+)\)', content):
        href = m.group(1)
        if href.startswith("http://") or href.startswith("https://"):
            continue
        file_part = href.split("#")[0]
        if file_part:
            links.append(file_part)
    return links


def _parent_folder_filenames():
    """Compute parent-folder planning filenames.

    Falls back to hardcoded list if parent folder is not present (e.g. CI).
    """
    if PARENT_DIR.is_dir():
        names = set()
        for p in PARENT_DIR.iterdir():
            if p.name == REPO.name:
                continue
            if p.is_file():
                names.add(p.name)
            elif p.is_dir() and not p.name.startswith("."):
                names.add(p.name)
        return names | _HARDCODED_PARENT_FILENAMES
    return _HARDCODED_PARENT_FILENAMES


# ---------------------------------------------------------------------------
# Drift gates
# ---------------------------------------------------------------------------

class TestDrift(unittest.TestCase):
    """Regenerating each generated page must produce byte-identical content."""

    def _caps(self):
        import sys
        sys.path.insert(0, str(REPO))
        from spoolctl.cli import build_parser, _SUBPARSERS
        from spoolctl.contract import build_capabilities
        build_parser()
        data, _ = build_capabilities(_SUBPARSERS)
        return data

    def _load_build_docs(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "build_docs", REPO / "scripts" / "build_docs.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _check_page(self, name):
        mod = self._load_build_docs()
        rel_path, builder = mod.PAGES[name]
        path = REPO / rel_path
        self.assertTrue(path.exists(), f"{rel_path} does not exist")
        caps = self._caps()
        expected = builder(caps)
        actual = path.read_text()
        self.assertEqual(actual, expected, f"{rel_path} is stale; regenerate with: python3 scripts/build_docs.py")

    def test_verbs_drift(self):
        self._check_page("verbs")

    def test_errors_drift(self):
        self._check_page("errors")

    def test_limits_drift(self):
        self._check_page("limits")

    def test_states_drift(self):
        self._check_page("states")

    def test_tz_stability(self):
        """Output must be stable across two consecutive runs."""
        mod = self._load_build_docs()
        caps = self._caps()
        for name, (_, builder) in mod.PAGES.items():
            content1 = builder(caps)
            content2 = builder(caps)
            self.assertEqual(content1, content2, f"{name} not stable across consecutive runs")


# ---------------------------------------------------------------------------
# Coverage gates
# ---------------------------------------------------------------------------

class TestCoverage(unittest.TestCase):
    """Every contract element must appear in its generated page."""

    @classmethod
    def setUpClass(cls):
        import sys
        sys.path.insert(0, str(REPO))
        from spoolctl.cli import build_parser, _SUBPARSERS
        from spoolctl.contract import build_capabilities
        from spoolctl import models as m
        build_parser()
        cls.caps, _ = build_capabilities(_SUBPARSERS)
        cls.models = m

    def test_every_verb_in_verbs_md(self):
        content = (DOCS / "verbs.md").read_text()
        for verb in self.caps["verbs"]:
            self.assertIn(f"## `{verb}`", content,
                          f"verb '{verb}' missing from docs/verbs.md")

    def test_every_error_code_in_errors_md(self):
        content = (DOCS / "errors.md").read_text()
        for code in self.caps["error_codes"]:
            self.assertIn(f"`{code}`", content,
                          f"error code '{code}' missing from docs/errors.md")

    def test_every_exit_code_in_errors_md(self):
        content = (DOCS / "errors.md").read_text()
        for code_str in self.caps["exit_codes"]:
            self.assertIn(f"| {code_str} |", content,
                          f"exit code {code_str} missing from docs/errors.md")

    def test_every_failure_reason_in_errors_md(self):
        content = (DOCS / "errors.md").read_text()
        for reason in self.models.FAILURE_REASONS:
            self.assertIn(f"`{reason}`", content,
                          f"failure reason '{reason}' missing from docs/errors.md")

    def test_every_job_state_in_states_md(self):
        content = (DOCS / "reference" / "states.md").read_text()
        for state in self.caps["job_states"]:
            self.assertIn(f"`{state}`", content,
                          f"job state '{state}' missing from docs/reference/states.md")

    def test_every_attempt_state_in_states_md(self):
        content = (DOCS / "reference" / "states.md").read_text()
        for state in self.caps["attempt_states"]:
            self.assertIn(f"`{state}`", content,
                          f"attempt state '{state}' missing from docs/reference/states.md")

    def test_every_env_var_in_limits_md(self):
        content = (DOCS / "reference" / "limits.md").read_text()
        for var in self.caps["env_vars"]:
            self.assertIn(f"`{var}`", content,
                          f"env var '{var}' missing from docs/reference/limits.md")


# ---------------------------------------------------------------------------
# Structural gates
# ---------------------------------------------------------------------------

class TestStructural(unittest.TestCase):
    """Structural checks for docs pages."""

    def test_generated_pages_exist(self):
        for path in GENERATED_PAGES:
            self.assertTrue(path.exists(), f"Generated page missing: {path.relative_to(REPO)}")

    def test_generated_pages_have_header(self):
        for path in GENERATED_PAGES:
            content = path.read_text()
            self.assertTrue(
                content.startswith("<!-- Generated by scripts/build_docs.py."),
                f"{path.name} missing generator header"
            )

    def test_generated_pages_are_versionless(self):
        """Generated pages must not contain TOOL_VERSION."""
        from spoolctl.models import TOOL_VERSION
        for path in GENERATED_PAGES:
            content = path.read_text()
            self.assertNotIn(TOOL_VERSION, content,
                             f"{path.name} contains TOOL_VERSION {TOOL_VERSION}")

    def test_no_mkdocs_only_syntax(self):
        for path in _all_docs_md():
            content = path.read_text()
            rel = path.relative_to(REPO)
            for lineno, line in enumerate(content.splitlines(), 1):
                stripped = line.lstrip()
                self.assertFalse(stripped.startswith("!!!"),
                                 f"{rel}:{lineno}: mkdocs admonition syntax")
                self.assertFalse(stripped.startswith("???"),
                                 f"{rel}:{lineno}: mkdocs collapsible syntax")
                self.assertFalse(stripped.startswith('=== "'),
                                 f"{rel}:{lineno}: mkdocs tab syntax")
                self.assertFalse(stripped.startswith("--8<--"),
                                 f"{rel}:{lineno}: mkdocs snippet syntax")

    def test_all_docs_links_resolve(self):
        for path in _all_docs_md():
            content = path.read_text()
            links = _extract_md_links(content)
            for link in links:
                target = (path.parent / link).resolve()
                self.assertTrue(
                    target.exists(),
                    f"{path.relative_to(REPO)}: broken link '{link}'"
                )

    def test_readme_links_into_docs_resolve(self):
        readme = REPO / "README.md"
        content = readme.read_text()
        links = _extract_md_links(content)
        docs_links = [l for l in links if l.startswith("docs/")]
        self.assertTrue(len(docs_links) > 0, "README has no links into docs/")
        for link in docs_links:
            target = (REPO / link).resolve()
            self.assertTrue(
                target.exists(),
                f"README.md: broken link '{link}'"
            )


class TestNav(unittest.TestCase):
    """mkdocs.yml nav tests."""

    def test_nav_parser_works(self):
        entries = _parse_mkdocs_nav()
        self.assertIsNotNone(entries, "mkdocs.yml not found")
        self.assertGreater(len(entries), 0, "nav has no entries")

    def test_every_docs_page_in_nav(self):
        entries = _parse_mkdocs_nav()
        if entries is None:
            self.skipTest("mkdocs.yml not found")

        docs_pages = set()
        for path in _all_docs_md():
            rel = str(path.relative_to(DOCS))
            docs_pages.add(rel)

        for page in docs_pages:
            self.assertIn(page, entries,
                          f"docs/{page} not in mkdocs nav")

    def test_every_nav_entry_exists(self):
        entries = _parse_mkdocs_nav()
        if entries is None:
            self.skipTest("mkdocs.yml not found")

        for entry in entries:
            path = DOCS / entry
            self.assertTrue(
                path.exists(),
                f"nav entry '{entry}' points to missing file"
            )

    def test_no_changelog_nav_entry(self):
        """v0.4.8: no changelog.md, no changelog nav entry."""
        entries = _parse_mkdocs_nav()
        if entries is None:
            self.skipTest("mkdocs.yml not found")

        changelog = DOCS / "changelog.md"
        if changelog.exists():
            self.assertIn("changelog.md", entries,
                          "changelog.md exists but is not in nav")
        else:
            self.assertNotIn("changelog.md", entries,
                             "changelog.md does not exist but is in nav")


class TestLeakGate(unittest.TestCase):
    """Private content leak gate."""

    def _check_no_absolute_paths(self, path, content):
        rel = path.relative_to(REPO)
        self.assertNotRegex(content, r"/Users/",
                            f"{rel} contains /Users/ path")
        self.assertNotRegex(content, r"/home/",
                            f"{rel} contains /home/ path")

    def _check_no_parent_filenames(self, path, content):
        rel = path.relative_to(REPO)
        parent_names = _parent_folder_filenames()
        for name in parent_names:
            if name.endswith((".md", ".txt")):
                self.assertNotIn(name, content,
                                 f"{rel} references parent-folder file '{name}'")

    def test_generated_pages_no_absolute_paths(self):
        for path in GENERATED_PAGES:
            self._check_no_absolute_paths(path, path.read_text())

    def test_generated_pages_no_parent_filenames(self):
        for path in GENERATED_PAGES:
            self._check_no_parent_filenames(path, path.read_text())

    def test_handwritten_pages_full_leak_gate(self):
        beads_id_pattern = re.compile(r'\bspoolctl-[a-z0-9]{3}\b')
        forward_version_pattern = re.compile(
            r'\bv0\.5\b|\bv0\.6\b|\b0\.5\.x\b|'
            r'\bnext release\b|\bdeferred to\b|\broadmap\b',
            re.IGNORECASE,
        )

        for path in _all_docs_md():
            if _is_generated(path):
                continue
            content = path.read_text()
            rel = path.relative_to(REPO)

            self._check_no_absolute_paths(path, content)
            self._check_no_parent_filenames(path, content)

            # Beads IDs
            matches = beads_id_pattern.findall(content)
            self.assertEqual(
                matches, [],
                f"{rel} contains Beads ID(s): {matches}"
            )

            # Forward-version language
            for line in content.splitlines():
                if any(allowed in line for allowed in _FORWARD_VERSION_ALLOWLIST):
                    continue
                m = forward_version_pattern.search(line)
                if m:
                    self.fail(
                        f"{rel}: forward-version language '{m.group()}' in: {line.strip()}"
                    )

    def test_readme_leak_gate(self):
        readme = REPO / "README.md"
        content = readme.read_text()
        self._check_no_absolute_paths(readme, content)


class TestContentAssertions(unittest.TestCase):
    """Thin content assertions for contract facts readers get wrong."""

    def test_doctor_exit_contract(self):
        content = (DOCS / "doctor.md").read_text()
        self.assertIn("3", content)
        self.assertIn("true", content.lower())
        self.assertIn("false", content.lower())
        self.assertIn("ready", content)

    def test_agent_guide_wait_contract(self):
        content = (DOCS / "agent-guide.md").read_text()
        self.assertIn("6", content)
        self.assertIn("all_succeeded", content)


if __name__ == "__main__":
    unittest.main()
