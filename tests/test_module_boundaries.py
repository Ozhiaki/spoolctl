"""Import-boundary guardrails for the v0.4.6 extraction."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "spoolctl"

FORBIDDEN_IMPORTS = {
    "spoolctl.contract": {"spoolctl.cli", "spoolctl.validation"},
    "spoolctl.models": {"spoolctl.cli", "spoolctl.contract", "spoolctl.errors",
                         "spoolctl.operations", "spoolctl.schemas",
                         "spoolctl.store", "spoolctl.validation", "spoolctl.worker"},
    "spoolctl.operations": {"spoolctl.cli"},
    "spoolctl.validation": {"spoolctl.cli"},
}

FORBIDDEN_REFERENCES = {
    "spoolctl.contract": {"cli.", "validation.", "spoolctl.cli", "spoolctl.validation"},
    "spoolctl.operations": {"cli.", "spoolctl.cli"},
    "spoolctl.validation": {"cli.", "spoolctl.cli"},
}


def module_path(module: str) -> Path:
    return PACKAGE / (module.removeprefix("spoolctl.") + ".py")


def spoolctl_imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "spoolctl" or alias.name.startswith("spoolctl."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "spoolctl":
                found.update(f"spoolctl.{alias.name}" for alias in node.names)
            elif node.module and node.module.startswith("spoolctl."):
                found.add(node.module)
    return found


def string_references(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
    return found


class TestModuleBoundaries(unittest.TestCase):
    def test_forbidden_spoolctl_import_edges(self):
        for module, forbidden in FORBIDDEN_IMPORTS.items():
            path = module_path(module)
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = spoolctl_imports(tree)
            self.assertFalse(
                imports & forbidden,
                f"{module} has forbidden imports: {sorted(imports & forbidden)}",
            )

    def test_forbidden_annotation_and_string_references(self):
        for module, forbidden in FORBIDDEN_REFERENCES.items():
            path = module_path(module)
            if not path.exists():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            strings = string_references(tree)
            offenders = [
                text for text in strings
                if any(fragment in text for fragment in forbidden)
            ]
            self.assertFalse(offenders, f"{module} has forbidden string refs: {offenders}")


if __name__ == "__main__":
    unittest.main()
