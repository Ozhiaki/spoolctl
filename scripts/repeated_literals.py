#!/usr/bin/env python3
"""Find extraction-sensitive repeated literals in the package."""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "spoolctl"

MIN_STRING_LEN = 4
KNOWN_EXTRACTION_LITERALS = {
    "canceled",
    "dead",
    "done",
    "^[A-Za-z0-9_.:-]+$",
    "^[A-Za-z0-9][A-Za-z0-9._-]*$",
}


def interesting(value: object) -> bool:
    if isinstance(value, str):
        return len(value) >= MIN_STRING_LEN or value in KNOWN_EXTRACTION_LITERALS
    if isinstance(value, (int, float)):
        return abs(value) >= 128
    return False


def scan(path: Path = PACKAGE) -> dict[str, list[str]]:
    files = [path] if path.is_file() else sorted(path.glob("*.py"))
    locations: dict[object, set[int]] = defaultdict(set)
    rendered_locations: dict[object, set[str]] = defaultdict(set)
    for file_path in files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
        rel = file_path.relative_to(REPO)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and interesting(node.value):
                locations[node.value].add(node.lineno)
                rendered_locations[node.value].add(f"{rel}:{node.lineno}")
    repeated = {
        repr(value): sorted(rendered_locations[value])
        for value, lines in locations.items()
        if len(lines) > 1 or value in KNOWN_EXTRACTION_LITERALS
    }
    return dict(sorted(repeated.items()))


def main() -> int:
    print(json.dumps(scan(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
