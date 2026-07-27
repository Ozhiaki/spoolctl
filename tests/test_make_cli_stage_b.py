"""Stage B make-cli probes generated from capabilities."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spoolctl import schemas

REPO = Path(__file__).resolve().parents[1]
TIMEOUT = 5


class SchemaError(AssertionError):
    pass


def matches_type(value: Any, typ: str) -> bool:
    if typ == "null":
        return value is None
    if typ == "boolean":
        return isinstance(value, bool)
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ == "string":
        return isinstance(value, str)
    if typ == "array":
        return isinstance(value, list)
    if typ == "object":
        return isinstance(value, dict)
    raise SchemaError(f"unknown type {typ!r}")


def validate(value: Any, schema: dict, path: str = "$") -> None:
    if not schema:
        return
    if "const" in schema and value != schema["const"]:
        raise SchemaError(f"{path}: expected const {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path}: {value!r} not in enum {schema['enum']!r}")
    if "oneOf" in schema:
        matches = 0
        for subschema in schema["oneOf"]:
            try:
                validate(value, subschema, path)
            except SchemaError:
                pass
            else:
                matches += 1
        if matches != 1:
            raise SchemaError(f"{path}: oneOf matched {matches}")
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


@dataclass
class Probe:
    desc: str
    category: str
    argv: list[str]
    expect: dict[str, Any]
    covers: set[tuple[str, str, str]] = field(default_factory=set)
    env: dict[str, str] = field(default_factory=dict)
    setup: list[list[str]] = field(default_factory=list)
    raw_prefix: bool = False


def run_proc(argv: list[str], *, env: dict[str, str] | None = None,
             timeout: float = TIMEOUT) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "spoolctl", *argv],
        cwd=REPO,
        env=merged,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def capabilities() -> dict:
    proc = run_proc(["capabilities", "--json"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)["data"]
    validate(data, schemas.VERB_SCHEMAS["capabilities"])
    return data


def has_flag(verb: dict, flag: str) -> bool:
    return any(f["name"] == flag for f in verb["flags"])


def base(verb: str, caps: dict, db: str, *, json_mode: bool = True) -> list[str]:
    argv = [verb]
    if has_flag(caps["verbs"][verb], "--db"):
        argv += ["--db", db]
    if json_mode and has_flag(caps["verbs"][verb], "--json"):
        argv.append("--json")
    return argv


def valid_positionals(verb: str) -> list[str]:
    if verb == "add":
        return ["--", "true"]
    if verb == "robot-docs":
        return ["guide"]
    if verb == "wait":
        return ["1"]
    if verb in {"cancel", "output", "retry", "show"}:
        return ["1"]
    return []


def valid_optional_positionals(verb: str) -> list[str]:
    if verb == "config-validate":
        return ["missing-config.json"]
    return []


def valid_required_options(verb: str, *, exclude: str | None = None) -> list[str]:
    if verb == "prune" and exclude != "--older-than":
        return ["--older-than", "1s"]
    return []


def setup_for_success(verb: str, mode: str, db: str) -> list[list[str]]:
    if verb == "add":
        return []
    if verb == "events" and mode == "frames":
        return [["add", "--db", db, "--json", "--", "true"]]
    if verb == "cancel":
        return [["add", "--db", db, "--json", "--", "sleep", "1"]]
    if verb == "output":
        return [
            ["add", "--db", db, "--json", "--", "sh", "-c", "printf hi"],
            ["work", "--db", db, "--json", "--once"],
        ]
    if verb == "retry":
        return [
            ["add", "--db", db, "--json", "--max-retries", "0", "--", "false"],
            ["work", "--db", db, "--json", "--once"],
        ]
    if verb == "show":
        return [["add", "--db", db, "--json", "--", "true"]]
    if verb == "wait":
        return [
            ["add", "--db", db, "--json", "--", "true"],
            ["work", "--db", db, "--json", "--once"],
        ]
    if verb == "work":
        return [["add", "--db", db, "--json", "--", "true"]]
    if verb == "doctor":
        return [["status", "--db", db, "--json"]]
    return []


def success_argv(verb: str, mode: str, caps: dict, db: str) -> list[str]:
    json_mode = mode == "envelope"
    if verb == "robot-docs":
        return ["robot-docs", "guide", "--json"] if json_mode else ["robot-docs", "guide"]
    argv = base(verb, caps, db, json_mode=json_mode)
    if verb == "add":
        return argv + ["--", "true"]
    if verb == "cancel":
        return argv + ["1"]
    if verb == "events":
        if mode == "frames":
            return base(verb, caps, db, json_mode=True) + [
                "--follow", "--since-id", "0", "--max-events", "1",
            ]
        return argv + ["--limit", "1"]
    if verb == "list":
        return argv + ["--limit", "1"]
    if verb == "output":
        if mode == "raw":
            return argv + ["1", "--stream", "stdout", "--raw"]
        return argv + ["1"]
    if verb == "prune":
        return argv + ["--older-than", "0", "--dry-run"]
    if verb == "retry":
        return argv + ["1"]
    if verb == "schema":
        return argv
    if verb == "show":
        return argv + ["1"]
    if verb == "status":
        return argv + ["--limit", "1"]
    if verb == "wait":
        return argv + ["1"]
    if verb == "work":
        return argv + ["--once"]
    return argv


def invalid_value_for(flag: dict, caps: dict) -> str | None:
    if flag["choices"]:
        return str(flag["choices"][0]) + "x"
    vocab = caps["probe_vocabularies"].get(flag["type"], {})
    values = vocab.get("invalid") or []
    if flag["type"] == "key_value":
        if flag["name"] == "--env":
            return "missing-equals"
        return "bad key=value"
    for value in values:
        if value:
            return value
    if flag["type"] == "integer":
        return "1.0"
    if flag["type"] == "float":
        return "nan"
    return None


def generated_probes(caps: dict, db: str) -> list[Probe]:
    probes: list[Probe] = []
    for verb_name, verb in sorted(caps["verbs"].items()):
        probes.append(Probe(
            desc=f"{verb_name}: unknown flag",
            category="unknown_flag",
            argv=base(verb_name, caps, db) + ["--zzprobe"]
            + valid_required_options(verb_name) + valid_positionals(verb_name),
            expect={"exit_code": 1, "ok": False, "error_code": "UNKNOWN_FLAG"},
            covers={(verb_name, "verb", verb_name)},
        ))
        for flag in verb["flags"]:
            name = flag["name"]
            if flag["value_required"]:
                probes.append(Probe(
                    desc=f"{verb_name}: {name} missing value",
                    category="missing_value",
                    argv=base(verb_name, caps, db) + [name],
                    expect={"exit_code": 1, "ok": False, "error_code": "INVALID_INPUT"},
                    covers={(verb_name, "flag", name)},
                ))
                bad = invalid_value_for(flag, caps)
                if bad is not None and name != "--db" and "bad_type" in flag["malformed_expectations"]:
                    probes.append(Probe(
                        desc=f"{verb_name}: {name} bad {flag['type']}",
                        category="bad_type",
                        argv=base(verb_name, caps, db) + [name, bad]
                        + valid_required_options(verb_name, exclude=name)
                        + valid_positionals(verb_name),
                        expect={
                            "exit_code": flag["malformed_expectations"]
                            .get("bad_type", {"exit_code": 1})["exit_code"],
                            "ok": False,
                            "error_code": flag["malformed_expectations"]
                            .get("bad_type", {"code": "INVALID_INPUT"})["code"],
                        },
                        covers={(verb_name, "flag", name), (flag["type"], "type", bad)},
                    ))
            else:
                probes.append(Probe(
                    desc=f"{verb_name}: {name} presence covered",
                    category="flag_presence",
                    argv=base(verb_name, caps, db) + [name] + valid_positionals(verb_name),
                    expect={"exit_code": None, "ok": None},
                    covers={(verb_name, "flag", name)},
                ))
        for arg in verb["args"]:
            if arg["required"]:
                probes.append(Probe(
                    desc=f"{verb_name}: missing positional {arg['name']}",
                    category="missing_positional",
                    argv=base(verb_name, caps, db),
                    expect={
                        "exit_code": arg["malformed_expectations"]["missing_value"]["exit_code"],
                        "ok": False,
                        "error_code": arg["malformed_expectations"]["missing_value"]["code"],
                    },
                    covers={(verb_name, "arg", arg["name"])},
                ))
            else:
                optionals = valid_optional_positionals(verb_name)
                if optionals:
                    probes.append(Probe(
                        desc=f"{verb_name}: optional positional {arg['name']}",
                        category="optional_positional",
                        argv=base(verb_name, caps, db)
                        + valid_required_options(verb_name)
                        + optionals,
                        expect={"exit_code": 0, "ok": True},
                        covers={(verb_name, "arg", arg["name"])},
                    ))
        for flags in verb["mutually_exclusive"]:
            argv = base(verb_name, caps, db)
            argv += valid_required_options(verb_name)
            for flag in flags:
                argv.append(flag)
                meta = next(f for f in verb["flags"] if f["name"] == flag)
                if meta["value_required"]:
                    argv.append(caps["probe_vocabularies"].get(meta["type"], {}).get("valid", "1"))
            argv += valid_positionals(verb_name)
            probes.append(Probe(
                desc=f"{verb_name}: mutually exclusive {'+'.join(flags)}",
                category="mutually_exclusive",
                argv=argv,
                expect={"exit_code": 1, "ok": False, "error_code": "INVALID_INPUT"},
                covers={(verb_name, "mutex", "+".join(flags))},
            ))
        for mode in verb["output_modes"]:
            argv = success_argv(verb_name, mode, caps, db)
            probes.append(Probe(
                desc=f"{verb_name}: {mode} success",
                category="output_mode_success",
                argv=argv,
                expect={"exit_code": 0, "ok": True, "mode": mode},
                covers={(verb_name, "output_mode", mode)},
                setup=setup_for_success(verb_name, mode, db),
                raw_prefix=False,
            ))
    for name, env_meta in sorted(caps["env_vars"].items()):
        verb_name = env_meta["consumed_by"][0]
        env = {name: "nan" if env_meta["type"] == "float" else db}
        argv = base(verb_name, caps, db)
        if name == "SPOOLCTL_DB":
            parent_file = os.path.join(os.path.dirname(db), "parent-file")
            env[name] = os.path.join(parent_file, "queue.db")
            argv = [verb_name, "--json"]
        argv += valid_positionals(verb_name)
        probes.append(Probe(
            desc=f"env var {name} malformed",
            category="env_var",
            argv=argv,
            env=env,
            expect={
                "exit_code": next(iter(env_meta["malformed_expectations"].values()))["exit_code"],
                "ok": False,
                "error_code": next(iter(env_meta["malformed_expectations"].values()))["code"],
            },
            covers={(name, "env_var", verb_name)},
            setup=[[sys.executable, "-c", f"open({parent_file!r}, 'w').close()"]]
            if name == "SPOOLCTL_DB" else [],
        ))
    return probes


class TestGeneratedStageBProbes(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "queue.db")
        self.caps = capabilities()
        self.probes = generated_probes(self.caps, self.db)

    def test_probe_coverage_report_has_floors(self):
        counts: dict[str, int] = {}
        covered = set()
        for probe in self.probes:
            counts[probe.category] = counts.get(probe.category, 0) + 1
            covered |= probe.covers
            if probe.expect.get("ok") is False:
                self.assertIn("error_code", probe.expect, probe.desc)

        expected_flags = {
            (verb_name, "flag", flag["name"])
            for verb_name, verb in self.caps["verbs"].items()
            for flag in verb["flags"]
        }
        expected_args = {
            (verb_name, "arg", arg["name"])
            for verb_name, verb in self.caps["verbs"].items()
            for arg in verb["args"]
        }
        expected_env = {
            (name, "env_var", meta["consumed_by"][0])
            for name, meta in self.caps["env_vars"].items()
        }
        expected_modes = {
            (verb_name, "output_mode", mode)
            for verb_name, verb in self.caps["verbs"].items()
            for mode in verb["output_modes"]
        }
        report = {
            "counts": counts,
            "total": len(self.probes),
            "covered": len(covered),
        }
        self.assertGreaterEqual(report["total"], 90, report)
        self.assertGreaterEqual(counts.get("bad_type", 0), 20, report)
        self.assertGreaterEqual(counts.get("output_mode_success", 0), 30, report)
        self.assertLessEqual(expected_flags, covered, report)
        self.assertLessEqual(expected_args, covered, report)
        self.assertLessEqual(expected_env, covered, report)
        self.assertLessEqual(expected_modes, covered, report)

    def run_setup(self, commands: list[list[str]]) -> None:
        for command in commands:
            if command[:2] == [sys.executable, "-c"]:
                proc = subprocess.run(command, text=True, capture_output=True, timeout=TIMEOUT)
            else:
                proc = run_proc(command)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def materialize(self, probe: Probe) -> Probe:
        unique_db = os.path.join(
            self.tmp.name,
            "probe-" + str(abs(hash(probe.desc))) + ".db",
        )

        def subst(value: str) -> str:
            return value.replace(self.db, unique_db)

        return Probe(
            desc=probe.desc,
            category=probe.category,
            argv=[subst(arg) for arg in probe.argv],
            expect=probe.expect,
            covers=probe.covers,
            env={key: subst(value) for key, value in probe.env.items()},
            setup=[[subst(arg) for arg in command] for command in probe.setup],
            raw_prefix=probe.raw_prefix,
        )

    def assert_probe(self, probe: Probe) -> None:
        probe = self.materialize(probe)
        self.run_setup(probe.setup)
        start = time.monotonic()
        if probe.raw_prefix:
            proc = subprocess.Popen(
                [sys.executable, "-m", "spoolctl", *probe.argv],
                cwd=REPO,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                if probe.argv and probe.argv[0] == "events":
                    db = probe.argv[probe.argv.index("--db") + 1]
                    time.sleep(0.1)
                    run_proc(["add", "--db", db, "--json", "--", "true"])
                ready, _, _ = select.select([proc.stdout], [], [], TIMEOUT)
                self.assertTrue(ready, probe.desc)
                line = proc.stdout.readline()
                json.loads(line)
            finally:
                proc.terminate()
                proc.wait(timeout=TIMEOUT)
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
            return
        proc = run_proc(probe.argv, env=probe.env)
        self.assertLess(time.monotonic() - start, TIMEOUT, probe.desc)
        self.assertNotIn("Traceback", proc.stderr + proc.stdout, probe.desc)
        expected_exit = probe.expect.get("exit_code")
        if expected_exit is not None:
            self.assertEqual(proc.returncode, expected_exit, probe.desc + proc.stderr)
        if probe.expect.get("ok") is False:
            env = json.loads(proc.stdout)
            validate(env, schemas.ENVELOPE_SCHEMA)
            self.assertFalse(env["ok"], probe.desc)
            self.assertEqual(env["errors"][0]["code"], probe.expect["error_code"], probe.desc)
            self.assertEqual(env["errors"][0]["exit_code"], proc.returncode, probe.desc)
            self.assertTrue(proc.stderr.strip(), probe.desc)
        elif probe.expect.get("mode") == "envelope":
            env = json.loads(proc.stdout)
            validate(env, schemas.ENVELOPE_SCHEMA)
            self.assertTrue(env["ok"], probe.desc)
        elif probe.expect.get("mode") == "text":
            self.assertTrue(proc.stdout.strip(), probe.desc)
        elif probe.expect.get("mode") == "raw":
            self.assertTrue(proc.stdout, probe.desc)
        elif probe.expect.get("mode") == "frames":
            lines = [json.loads(line) for line in proc.stdout.splitlines()]
            self.assertGreaterEqual(len(lines), 2, probe.desc)
            self.assertIn("id", lines[0], probe.desc)
            self.assertNotIn("control", lines[0], probe.desc)
            self.assertNotIn("id", lines[-1], probe.desc)
            self.assertEqual(lines[-1]["control"]["type"], "end", probe.desc)
            for line in lines:
                self.assertNotEqual(
                    set(line),
                    {"ok", "tool_version", "data", "meta", "warnings", "commands", "errors"},
                    probe.desc,
                )

    def test_generated_failure_rows_execute(self):
        for probe in self.probes:
            if probe.expect.get("ok") is False:
                with self.subTest(probe=probe.desc):
                    self.assert_probe(probe)

    def test_generated_output_mode_success_rows_execute(self):
        for probe in self.probes:
            if probe.category == "output_mode_success":
                with self.subTest(probe=probe.desc):
                    self.assert_probe(probe)


if __name__ == "__main__":
    unittest.main()
