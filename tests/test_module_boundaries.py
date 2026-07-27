"""Import-boundary guardrails for package module ownership."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "spoolctl"

FORBIDDEN_IMPORTS = {
    "spoolctl.__main__": set(),
    "spoolctl.cli": set(),
    "spoolctl.config": {"spoolctl.cli"},
    "spoolctl.contract": {"spoolctl.cli", "spoolctl.validation"},
    "spoolctl.errors": set(),
    "spoolctl.models": {"spoolctl.cli", "spoolctl.contract", "spoolctl.errors",
                         "spoolctl.operations", "spoolctl.schemas",
                         "spoolctl.store", "spoolctl.validation", "spoolctl.worker"},
    "spoolctl.operations": {"spoolctl.cli"},
    "spoolctl.schemas": set(),
    "spoolctl.store": set(),
    "spoolctl.validation": {"spoolctl.cli"},
    "spoolctl.worker": set(),
}

FORBIDDEN_REFERENCES = {
    "spoolctl.__main__": set(),
    "spoolctl.cli": set(),
    "spoolctl.config": {"cli.", "spoolctl.cli"},
    "spoolctl.contract": {"cli.", "validation.", "spoolctl.cli", "spoolctl.validation"},
    "spoolctl.errors": set(),
    "spoolctl.models": set(),
    "spoolctl.operations": {"cli.", "spoolctl.cli"},
    "spoolctl.schemas": set(),
    "spoolctl.store": set(),
    "spoolctl.validation": {"cli.", "spoolctl.cli"},
    "spoolctl.worker": set(),
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

    def test_boundary_table_keys_resolve_to_modules(self):
        for table in (FORBIDDEN_IMPORTS, FORBIDDEN_REFERENCES):
            for module in table:
                self.assertTrue(module_path(module).exists(), module)

    def test_every_package_module_is_in_a_boundary_table(self):
        covered = set(FORBIDDEN_IMPORTS) | set(FORBIDDEN_REFERENCES)
        modules = {
            "spoolctl." + path.stem
            for path in PACKAGE.glob("*.py")
            if path.name != "__init__.py"
        }
        self.assertLessEqual(modules, covered)


if __name__ == "__main__":
    unittest.main()
