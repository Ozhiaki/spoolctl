"""JSON Schema conformance layer for the spoolctl machine contract."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from spoolctl import cli
from spoolctl import schemas

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
SUPPORTED = {
    "type", "properties", "required", "additionalProperties",
    "items", "enum", "oneOf", "const",
}


class SchemaError(AssertionError):
    pass


def check_subset(schema: Any, path: str = "$") -> None:
    if not isinstance(schema, dict):
        return
    for key, value in schema.items():
        if key not in SUPPORTED:
            raise SchemaError(f"{path}: unsupported keyword {key!r}")
        if key == "properties":
            for name, subschema in value.items():
                check_subset(subschema, f"{path}.properties.{name}")
        elif key in {"items", "additionalProperties"} and isinstance(value, dict):
            check_subset(value, f"{path}.{key}")
        elif key == "oneOf":
            for i, subschema in enumerate(value):
                check_subset(subschema, f"{path}.oneOf[{i}]")


def matches_type(value: Any, typ: str) -> bool:
    if typ == "null":
        return value is None
    if typ == "boolean":
        return isinstance(value, bool)
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
    if typ == "string":
        return isinstance(value, str)
    if typ == "array":
        return isinstance(value, list)
    if typ == "object":
        return isinstance(value, dict)
    raise SchemaError(f"unknown type {typ!r}")


def validate(value: Any, schema: dict, path: str = "$") -> None:
    check_subset(schema, path)
    if not schema:
        return
    if "const" in schema and value != schema["const"]:
        raise SchemaError(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: {value!r} not in enum {schema['enum']!r}")
    if "oneOf" in schema:
        matches = 0
        errors = []
        for subschema in schema["oneOf"]:
            try:
                validate(value, subschema, path)
            except SchemaError as exc:
                errors.append(str(exc))
            else:
                matches += 1
        if matches != 1:
            raise SchemaError(f"{path}: oneOf matched {matches}, errors={errors}")
        return
    if "type" in schema:
        types = schema["type"]
        if isinstance(types, str):
            types = [types]
        if not any(matches_type(value, typ) for typ in types):
            raise SchemaError(f"{path}: {value!r} does not match type {types!r}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError(f"{path}: missing required key {key!r}")
        for key, item in value.items():
            if key in props:
                validate(item, props[key], f"{path}.{key}")
            else:
                additional = schema.get("additionalProperties", True)
                if additional is False:
                    raise SchemaError(f"{path}: unexpected key {key!r}")
                if isinstance(additional, dict):
                    validate(item, additional, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{i}]")


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def load_envelope(raw: str) -> dict:
    return json.loads(raw)


GOLDEN_VERBS = {
    "envelope-add.json": "add",
    "envelope-work-once-empty.json": "work",
    "envelope-work-drain-empty.json": "work",
    "envelope-status-empty.json": "status",
    "envelope-list-empty.json": "list",
    "envelope-wait-mixed.json": "wait",
    "envelope-prune-empty.json": "prune",
    "envelope-cancel-queued.json": "cancel",
    "envelope-output-no-attempts.json": "output",
}


class TestSchemaSubset(unittest.TestCase):
    def test_all_exported_schemas_use_supported_subset(self):
        check_subset(schemas.ENVELOPE_SCHEMA)
        for schema in schemas.VERB_SCHEMAS.values():
            check_subset(schema)
        for schema in schemas.STREAM_SCHEMAS.values():
            check_subset(schema)

    def test_unknown_schema_keyword_rejected(self):
        with self.assertRaises(SchemaError):
            check_subset({"type": "string", "pattern": "x"})

    def test_nullable_array_type_is_accepted(self):
        schema = {"type": ["string", "null"]}
        validate(None, schema)
        validate("x", schema)
        with self.assertRaises(SchemaError):
            validate(1, schema)


class TestEnvelopeConformance(unittest.TestCase):
    def test_golden_envelopes_validate(self):
        for path in sorted(GOLDEN_DIR.glob("envelope-*.json")):
            env = load_envelope(path.read_text())
            validate(env, schemas.ENVELOPE_SCHEMA, path.name)
            verb = GOLDEN_VERBS.get(path.name)
            if verb and env["ok"]:
                validate(env["data"], schemas.VERB_SCHEMAS[verb], path.name + ".data")

    def test_capabilities_data_validates(self):
        env = load_envelope((GOLDEN_DIR / "capabilities.json").read_text())
        validate(env, schemas.VERB_SCHEMAS["capabilities"])

    def test_optional_meta_shapes_validate(self):
        base = cli.make_envelope({"count": 0, "events": []}, started=0.0)
        validate(base, schemas.ENVELOPE_SCHEMA)
        paginated = cli.make_envelope(
            {"count": 0, "events": []},
            started=0.0,
            meta_extra={"pagination": {"cursor": 0, "first_id": None}},
        )
        validate(paginated, schemas.ENVELOPE_SCHEMA)
        waiting = cli.make_envelope(
            {"count": 0, "events": []},
            started=0.0,
            meta_extra={
                "pagination": {"cursor": 0, "first_id": None},
                "wait": {"waited_ms": 30, "reason": "timeout"},
            },
        )
        validate(waiting, schemas.ENVELOPE_SCHEMA)


class TestLiveConformance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")

    def assert_verb(self, verb: str, *argv: str, exit_code: int = 0) -> dict:
        code, out, err = run_cli(*argv)
        self.assertEqual(code, exit_code, err)
        env = load_envelope(out)
        validate(env, schemas.ENVELOPE_SCHEMA)
        if env["ok"]:
            validate(env["data"], schemas.VERB_SCHEMAS[verb], verb)
        return env

    def test_live_existing_outputs_validate(self):
        add = self.assert_verb("add", "add", "--db", self.db, "--json", "--", "true")
        job_id = str(add["data"]["job_id"])
        self.assert_verb("work", "work", "--once", "--db", self.db, "--json")
        self.assert_verb("show", "show", job_id, "--db", self.db, "--json")
        self.assert_verb("list", "list", "--db", self.db, "--json")
        self.assert_verb("wait", "wait", job_id, "--db", self.db, "--json")
        self.assert_verb("output", "output", job_id, "--db", self.db, "--json")
        self.assert_verb("status", "status", "--db", self.db, "--json")
        self.assert_verb("prune", "prune", "--older-than", "0", "--db", self.db, "--json")
        self.assert_verb("capabilities", "capabilities", "--json")


if __name__ == "__main__":
    unittest.main()
