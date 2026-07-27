"""Machine-readable contract metadata and builders.

This module owns capability descriptions, schema exports, brief text, and
robot documentation data. It intentionally does not import the CLI adapter.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from spoolctl import schemas
from spoolctl.errors import CliError
from spoolctl.models import (
    ATTEMPT_STATES,
    CODE_REGISTRY,
    CONTRACT_VERSION,
    EVENT_LIMIT_MAX,
    ERROR_CODES,
    EXIT_CODES,
    EXIT_CONFLICT,
    EXIT_INPUT,
    EXIT_SAFETY,
    FAILURE_REASONS,
    HEARTBEAT_INTERVAL,
    JOB_EVENT_TYPES,
    JOB_STATES,
    LIST_LIMIT_MAX,
    MAX_DURATION_SECONDS,
    MAX_ENV_KEY_CHARS,
    MAX_ENV_VALUE_CHARS,
    MAX_PATH_CHARS,
    MAX_POLL_INTERVAL_SECONDS,
    MAX_WAIT_SECONDS,
    MAX_WORKER_ID_CHARS,
    PRIORITY_MAX,
    PRIORITY_MIN,
    REAP_THRESHOLD,
    SQLITE_INT64_MAX,
    SQLITE_INT64_MIN,
    STATUS_LIMIT_MAX,
    TOOL_VERSION,
    VERBS,
    _suggest,
)

# One-line data-schema summaries per verb; the flags themselves are always
# introspected from the live parser, never hand-maintained.
VERB_SUMMARIES = {
    "add": {
        "summary": "enqueue a command; --key deduplicates active queued/running jobs",
        "data_schema": "{job_id: int, state: 'queued'|'running', deduplicated: bool,"
                       " next_run_at: float, priority: int, queue: str,"
                       " cwd: str|null, env_keys: [str], idempotency?:"
                       " {key, metadata_differs, metadata_differences}}",
    },
    "work": {
        "summary": "run jobs until stopped; --once runs at most one;"
                   " --drain runs until the queue settles; --queue serves one"
                   " lane; --slots optionally bounds running jobs in that lane",
        "data_schema": "--once: {claimed: bool, job_id?, attempt_no?, result?,"
                       " job_state?}; --drain: {drained: bool, executed: int};"
                       " loop mode writes nothing to stdout; claimed:false means"
                       " no job runnable by this worker right now, including a"
                       " full slot ceiling",
    },
    "wait": {
        "summary": "block until every given job settles (done/dead/canceled);"
                   " exit 0 all done, exit 6 any failed (envelope stays ok:true)",
        "data_schema": "{all_succeeded: bool, jobs: {'<id>': {state, attempts,"
                       " last_exit_code, last_error}}}",
    },
    "status": {
        "summary": "queue counts, scheduled sub-counts, per-lane counts, and recent dead jobs; always exit 0",
        "data_schema": "{counts: {canceled,dead,done,failed,queued,running},"
                       " scheduled: int, queues: {<queue>: {counts, scheduled}},"
                       " recent_dead: [{id, command, attempts, crashes, last_error,"
                       " finished_at, stdout_path, stderr_path}]}",
    },
    "list": {
        "summary": "enumerate jobs, newest first, optionally filtered by state/tag/queue/priority",
        "data_schema": "{count: int, jobs: [{id, argv, state, attempts,"
                       " crashes, max_retries, timeout_seconds, created_at,"
                       " started_at, finished_at, next_run_at, priority, queue,"
                       " cwd,"
                       " last_exit_code, last_error, idempotency_key, tags,"
                       " note}]}",
    },
    "show": {
        "summary": "one job in full detail: row, attempts, event trail",
        "data_schema": "{job: {id, argv, state, attempts, max_retries,"
                       " timeout_seconds, created_at, started_at, finished_at,"
                       " next_run_at, priority, queue, cwd, env, crashes,"
                       " max_crashes, locked_by, locked_pid,"
                       " locked_at, heartbeat_at, last_exit_code, last_error,"
                       " last_failure_reason,"
                       " idempotency_key, tags, note},"
                       " attempts: [{attempt_no, state, worker_id, worker_pid,"
                       " started_at, finished_at, exit_code, error,"
                       " failure_reason,"
                       " stdout_path, stderr_path}],"
                       " events: [{at, event, worker_id, detail}]}",
    },
    "retry": {
        "summary": "requeue a dead or failed job with a fresh retry budget",
        "data_schema": "{job_id: int, state: 'queued'}",
    },
    "cancel": {
        "summary": "cancel a queued job; --running also stops a running one"
                   " (killed by its owning worker within a heartbeat)",
        "data_schema": "{job_id: int, state: 'canceled', was_running: bool}",
    },
    "prune": {
        "summary": "delete terminal jobs older than a duration, files first"
                   " then rows; requires --yes unless --dry-run reports without deleting",
        "data_schema": "{matched: int, deleted_jobs: int, deleted_attempts:"
                       " int, deleted_events: int, freed_bytes: int,"
                       " dry_run: bool, actual: bool, irreversible?: bool}",
    },
    "output": {
        "summary": "captured stdout/stderr for any attempt of a job",
        "data_schema": "{attempt_no, attempt_state, attempts_total, job_id,"
                       " streams: {stdout|stderr: {path, preview,"
                       " preview_truncated, size_bytes}}} or {attempts: []}",
    },
    "events": {
        "summary": "read the durable event ledger; verify job ids with spoolctl show;"
                   " --follow --json emits NDJSON data frames plus end/error control frames",
        "data_schema": "{count: int, events: [{id, job_id, at, event, worker_id,"
                       " detail}]}; meta.pagination:{cursor, first_id};"
                       " --wait also adds meta.wait:{reason, waited_ms}",
    },
    "brief": {
        "summary": "compact db-free usage brief for agent context injection",
        "data_schema": "{text: str, approx_tokens: int, budget_tokens: 700}",
    },
    "schema": {
        "summary": "export JSON Schemas for the envelope, verb data payloads, and streams",
        "data_schema": "{dialect: str, envelope_schema: object,"
                       " verbs: {<name>: schema}, streams: {events_follow: schema}}",
    },
    "capabilities": {
        "summary": "this machine-readable contract",
        "data_schema": "{attempt_states, contract_policy, contract_version, env,"
                       " error_codes, events, exit_codes, failure_reasons, job_states,"
                       " scheduling, verbs}",
    },
    "config-show": {
        "summary": "show effective read-only project configuration and DB path source",
        "data_schema": "{config_path, config_exists, config_valid,"
                       " values:{db_path}, sources:{db_path}, precedence,"
                       " ignored_keys}",
    },
    "config-validate": {
        "summary": "validate project config JSON without opening the queue database",
        "data_schema": "{config_path, exists, valid, format, schema_version,"
                       " recognized_keys, unknown_keys}",
    },
    "robot-docs": {
        "summary": "agent workflow guide; currently supports the guide subcommand",
        "data_schema": "{text: str, approx_tokens: int, sections: [{title, bullets}]}",
    },
}

ENV_DOCS = {
    "SPOOLCTL_DB": "queue database path (overridden by --db; default"
                   " ./.spoolctl/queue.db)",
    "SPOOLCTL_TEST_HEARTBEAT_INTERVAL": "test-only: seconds between worker"
                                        " heartbeats (default 5)",
    "SPOOLCTL_TEST_REAP_THRESHOLD": "test-only: seconds of heartbeat staleness"
                                    " before a running job becomes a reap"
                                    " candidate (default 30)",
}

EXPECT_INVALID = {"code": "INVALID_INPUT", "exit_code": EXIT_INPUT}
EXPECT_MISSING = {"code": "MISSING_REQUIRED", "exit_code": EXIT_INPUT}
EXPECT_UNKNOWN_FLAG = {"code": "UNKNOWN_FLAG", "exit_code": EXIT_INPUT}
EXPECT_SAFETY = {"code": "SAFETY_BLOCK", "exit_code": EXIT_SAFETY}
EXPECT_CONFLICT = {"code": "IDEMPOTENCY_CONFLICT", "exit_code": EXIT_CONFLICT}

DB_VERBS = [
    "add",
    "cancel",
    "events",
    "list",
    "output",
    "prune",
    "retry",
    "show",
    "status",
    "wait",
    "work",
]

FEATURES = [
    "json_envelope",
    "schema_export",
    "did_you_mean",
    "event_ledger",
    "raw_output",
    "robot_docs",
    "idempotency_keys",
    "destructive_gates",
    "probeable_surface",
]

OUTPUT_MODES = ["envelope", "frames", "raw", "text"]

TOTALITY_CONTRACT = {
    "envelope": {
        "selector": "--json",
        "failure_stdout": "one parseable JSON envelope",
        "failure_stderr": "human mirror of the first error",
        "traceback_allowed": False,
    },
    "frames": {
        "implemented": True,
        "failure_stdout": "valid NDJSON frames when a frames surface exists",
        "traceback_allowed": False,
    },
    "raw": {
        "failure_stdout": "empty or raw payload only; diagnostics stay on stderr",
        "traceback_allowed": False,
    },
    "text": {
        "selector": "default",
        "failure_stdout": "not a machine surface",
        "failure_stderr": "human diagnostic",
        "traceback_allowed": False,
    },
}

PROBE_VOCABULARIES = {
    "boolean": {
        "invalid": [],
        "notes": "bool flags are presence-only; missing-value probes do not apply",
    },
    "duration": {
        "invalid": ["-1", "1D", "1e308", ""],
        "valid": "1s",
    },
    "integer": {
        "invalid": ["1_000", "0x10", "", " 1", "1.0"],
        "out_of_range_high": str(SQLITE_INT64_MAX + 1),
        "out_of_range_low": str(SQLITE_INT64_MIN - 1),
        "valid": "1",
    },
    "float": {
        "invalid": ["nan", "-nan", "inf", "-inf", "", "abc"],
        "valid": "0.1",
    },
    "key_value": {
        "invalid": ["missing-equals", "=value", "bad key=value"],
        "valid": "k=v",
    },
    "path": {
        "invalid": ["", "bad\x00path"],
        "valid": ".",
    },
    "queue": {
        "invalid": ["", " bad", "bad name", "-bad"],
        "valid": "default",
    },
    "string": {
        "invalid": ["", "\n"],
        "valid": "x",
    },
    "timestamp": {
        "invalid": ["not-a-time", "nan", "inf", "1e9999"],
        "valid": "1700000000",
    },
}

GLOBAL_FLAGS = [
    {
        "name": "--help",
        "flag": "--help",
        "aliases": ["--help"],
        "type": "boolean",
        "required": False,
        "default": False,
        "minimum": None,
        "maximum": None,
        "unbounded": False,
        "value_required": False,
        "repeatable": False,
        "choices": None,
        "malformed_expectations": {},
        "output_modes": ["text"],
    },
    {
        "name": "--version",
        "flag": "--version",
        "aliases": ["--version"],
        "type": "boolean",
        "required": False,
        "default": False,
        "minimum": None,
        "maximum": None,
        "unbounded": False,
        "value_required": False,
        "repeatable": False,
        "choices": None,
        "malformed_expectations": {},
        "output_modes": ["text"],
    },
]

LIMITS = {
    "attempt_number": {
        "type": "integer",
        "minimum": 1,
        "maximum": SQLITE_INT64_MAX,
        "unbounded": False,
    },
    "cwd_length": {
        "type": "path",
        "minimum": 1,
        "maximum": MAX_PATH_CHARS,
        "unbounded": False,
    },
    "duration_seconds": {
        "type": "duration",
        "minimum": 0,
        "maximum": MAX_DURATION_SECONDS,
        "unbounded": False,
    },
    "env_key_length": {
        "type": "string",
        "minimum": 1,
        "maximum": MAX_ENV_KEY_CHARS,
        "unbounded": False,
    },
    "env_value_length": {
        "type": "string",
        "minimum": 0,
        "maximum": MAX_ENV_VALUE_CHARS,
        "unbounded": False,
    },
    "id": {
        "type": "integer",
        "minimum": 1,
        "maximum": SQLITE_INT64_MAX,
        "unbounded": False,
    },
    "idempotency_key_length": {
        "type": "string",
        "minimum": 1,
        "maximum": 256,
        "charset": "printable after trim; no control characters",
        "unbounded": False,
    },
    "limit": {
        "type": "integer",
        "minimum": 0,
        "maximum": LIST_LIMIT_MAX,
        "unbounded": False,
    },
    "event_limit": {
        "type": "integer",
        "minimum": 0,
        "maximum": EVENT_LIMIT_MAX,
        "unbounded": False,
    },
    "status_limit": {
        "type": "integer",
        "minimum": 0,
        "maximum": STATUS_LIMIT_MAX,
        "unbounded": False,
    },
    "note_length": {
        "type": "string",
        "minimum": 0,
        "maximum": 10000,
        "unbounded": False,
    },
    "poll_interval_seconds": {
        "type": "float",
        "minimum": 0,
        "maximum": MAX_POLL_INTERVAL_SECONDS,
        "unbounded": False,
        "grammar": "finite float > 0",
    },
    "wait_seconds": {
        "type": "float",
        "minimum": 0,
        "maximum": MAX_WAIT_SECONDS,
        "unbounded": False,
        "grammar": "finite float > 0",
    },
    "priority": {
        "type": "integer",
        "minimum": PRIORITY_MIN,
        "maximum": PRIORITY_MAX,
        "unbounded": False,
    },
    "queue_name": {
        "type": "queue",
        "minimum": 1,
        "maximum": 64,
        "charset": "^[A-Za-z0-9][A-Za-z0-9._-]*$",
        "unbounded": False,
    },
    "tag_count": {
        "type": "integer",
        "minimum": 0,
        "maximum": 16,
        "unbounded": False,
    },
    "tag_key_length": {
        "type": "string",
        "minimum": 1,
        "maximum": 128,
        "charset": "^[A-Za-z0-9_.:-]+$",
        "unbounded": False,
    },
    "tag_value_length": {
        "type": "string",
        "minimum": 0,
        "maximum": 1024,
        "unbounded": False,
    },
    "timeout_seconds": {
        "type": "integer",
        "minimum": 1,
        "maximum": SQLITE_INT64_MAX,
        "unbounded": False,
    },
    "timestamp_epoch_seconds": {
        "type": "timestamp",
        "minimum": -MAX_DURATION_SECONDS,
        "maximum": MAX_DURATION_SECONDS,
        "unbounded": False,
        "grammar": "finite signed decimal epoch seconds or ISO-8601 timestamp",
    },
    "worker_slots": {
        "type": "integer",
        "minimum": 1,
        "maximum": SQLITE_INT64_MAX,
        "unbounded": False,
    },
    "worker_id_length": {
        "type": "string",
        "minimum": 1,
        "maximum": MAX_WORKER_ID_CHARS,
        "charset": "printable; no control characters",
        "unbounded": False,
    },
}

FLAG_CONTRACTS = {
    "--after": {"type": "duration", **LIMITS["duration_seconds"]},
    "--at": {"type": "timestamp", **LIMITS["timestamp_epoch_seconds"]},
    "--attempt": {"type": "integer", **LIMITS["attempt_number"]},
    "--db": {
        "type": "path",
        "minimum": 1,
        "maximum": MAX_PATH_CHARS,
        "unbounded": False,
        "malformed_expectations": {"bad_path": EXPECT_INVALID},
    },
    "--env": {
        "type": "key_value",
        "repeatable": True,
        "grammar": "K=V split on first '='; key non-empty; NUL rejected",
        "key_maximum": MAX_ENV_KEY_CHARS,
        "value_maximum": MAX_ENV_VALUE_CHARS,
        "malformed_expectations": {
            "bad_type": EXPECT_INVALID,
            "empty_string": EXPECT_INVALID,
        },
    },
    "--key": {
        "type": "string",
        "minimum": 1,
        "maximum": 256,
        "charset": "printable after trim; no control characters",
        "malformed_expectations": {"empty_string": EXPECT_INVALID},
        "unbounded": False,
    },
    "--limit": {"type": "integer", **LIMITS["limit"]},
    "--max-events": {"type": "integer", "minimum": 1, "maximum": SQLITE_INT64_MAX, "unbounded": False},
    "--idle-timeout": {"type": "float", **LIMITS["wait_seconds"]},
    "--max-crashes": {"type": "integer", "minimum": 0, "maximum": SQLITE_INT64_MAX, "unbounded": False},
    "--max-retries": {"type": "integer", "minimum": 0, "maximum": SQLITE_INT64_MAX, "unbounded": False},
    "--note": {
        "type": "string",
        "minimum": 0,
        "maximum": 10000,
        "malformed_expectations": {"too_long": EXPECT_INVALID},
        "unbounded": False,
    },
    "--older-than": {"type": "duration", **LIMITS["duration_seconds"]},
    "--poll-interval": {"type": "float", **LIMITS["poll_interval_seconds"]},
    "--priority": {"type": "integer", **LIMITS["priority"]},
    "--priority-min": {"type": "integer", **LIMITS["priority"]},
    "--queue": {"type": "queue", **LIMITS["queue_name"]},
    "--since-id": {"type": "integer", "minimum": 0, "maximum": SQLITE_INT64_MAX, "unbounded": False},
    "--state": {
        "type": "enum_csv",
        "choices": sorted(JOB_STATES),
        "malformed_expectations": {"bad_type": EXPECT_INVALID},
    },
    "--stream": {
        "type": "enum",
        "choices": ["both", "stderr", "stdout"],
        "malformed_expectations": {"bad_type": EXPECT_INVALID},
    },
    "--tag": {
        "type": "key_value",
        "repeatable": True,
        "grammar": "KEY=VALUE for add; KEY or KEY=VALUE for list; key matches [A-Za-z0-9_.:-]+",
        "maximum_items": 16,
        "key_maximum": 128,
        "value_maximum": 1024,
        "malformed_expectations": {"bad_type": EXPECT_INVALID},
    },
    "--timeout": {"type": "integer", **LIMITS["timeout_seconds"]},
    "--wait-timeout": {"type": "float", **LIMITS["wait_seconds"]},
    "--worker-id": {
        "type": "string",
        **LIMITS["worker_id_length"],
    },
    "--verb": {
        "type": "enum",
        "choices": sorted(VERBS),
        "malformed_expectations": {"bad_type": EXPECT_INVALID},
    },
}

VERB_FLAG_CONTRACTS = {
    ("prune", "--state"): {
        "type": "enum_csv",
        "choices": ["canceled", "dead", "done"],
        "malformed_expectations": {"bad_type": EXPECT_INVALID},
    },
    ("wait", "--timeout"): {"type": "float", **LIMITS["wait_seconds"]},
    ("events", "--limit"): {"type": "integer", **LIMITS["event_limit"]},
    ("events", "--max-events"): {"type": "integer", "minimum": 1, "maximum": EVENT_LIMIT_MAX, "unbounded": False},
    ("status", "--limit"): {"type": "integer", **LIMITS["status_limit"]},
}

POSITIONAL_CONTRACTS = {
    "id": {"type": "integer", **LIMITS["id"]},
    "ids": {"type": "integer", **LIMITS["id"]},
    "robot_docs_command": {
        "type": "enum",
        "choices": ["guide"],
        "required": True,
        "nargs": "1",
        "unbounded": False,
    },
    "argv": {
        "type": "string",
        "required": True,
        "minimum": 1,
        "maximum": None,
        "unbounded": True,
        "unbounded_reason": "command argv is stored as JSON and bounded by OS/SQLite limits",
    },
}

VERB_TRAITS = {
    "add": {"mutates": True, "destructive": False, "idempotent": "when --key is supplied"},
    "brief": {"mutates": False, "destructive": False, "idempotent": True},
    "cancel": {"mutates": True, "destructive": "only with --running", "idempotent": False},
    "capabilities": {"mutates": False, "destructive": False, "idempotent": True},
    "config-show": {"mutates": False, "destructive": False, "idempotent": True},
    "config-validate": {"mutates": False, "destructive": False, "idempotent": True},
    "robot-docs": {"mutates": False, "destructive": False, "idempotent": True},
    "events": {"mutates": False, "destructive": False, "idempotent": True},
    "list": {"mutates": False, "destructive": False, "idempotent": True},
    "output": {"mutates": False, "destructive": False, "idempotent": True},
    "prune": {"mutates": True, "destructive": True, "idempotent": False},
    "retry": {"mutates": True, "destructive": "only with --force on running jobs", "idempotent": False},
    "schema": {"mutates": False, "destructive": False, "idempotent": True},
    "show": {"mutates": False, "destructive": False, "idempotent": True},
    "status": {"mutates": False, "destructive": False, "idempotent": True},
    "wait": {"mutates": False, "destructive": False, "idempotent": True},
    "work": {"mutates": True, "destructive": False, "idempotent": False},
}

VERB_OUTPUT_MODES = {
    "events": ["envelope", "frames", "text"],
    "output": ["envelope", "raw", "text"],
}

VERB_MUTEX = {
    "add": [["--after", "--at"]],
    "events": [
        ["--wait", "--follow"],
        ["--limit", "--follow"],
        ["--max-events", "--wait"],
        ["--idle-timeout", "--wait"],
    ],
    "prune": [["--yes", "--dry-run"]],
    "work": [["--once", "--drain"]],
}

VERB_EXAMPLES = {
    "add": [["spoolctl", "add", "--json", "--key", "run-1", "--", "true"]],
    "brief": [["spoolctl", "brief", "--json"]],
    "cancel": [["spoolctl", "cancel", "--json", "1"]],
    "capabilities": [["spoolctl", "capabilities", "--json"]],
    "config-show": [["spoolctl", "config-show", "--json"]],
    "config-validate": [["spoolctl", "config-validate", "--json"]],
    "robot-docs": [["spoolctl", "robot-docs", "guide", "--json"]],
    "events": [["spoolctl", "events", "--json", "--limit", "10"]],
    "list": [["spoolctl", "list", "--json", "--limit", "10"]],
    "output": [["spoolctl", "output", "--json", "1", "--stream", "stdout"]],
    "prune": [["spoolctl", "prune", "--json", "--older-than", "30d", "--dry-run"]],
    "retry": [["spoolctl", "retry", "--json", "1"]],
    "schema": [["spoolctl", "schema", "--json"]],
    "show": [["spoolctl", "show", "--json", "1"]],
    "status": [["spoolctl", "status", "--json"]],
    "wait": [["spoolctl", "wait", "--json", "1"]],
    "work": [["spoolctl", "work", "--json", "--once"]],
}

PROBE_HINTS = {
    "categories": [
        "unknown_flag",
        "missing_value",
        "bad_type",
        "missing_positional",
        "mutually_exclusive",
        "env_var",
        "output_mode_success",
    ],
    "timeout_seconds": 5,
    "json_failure_verdict": {
        "ok": False,
        "stdout": "parseable envelope",
        "stderr": "non-empty mirror",
        "traceback_allowed": False,
    },
}

ENV_VAR_CONTRACTS = {
    "SPOOLCTL_DB": {
        "description": ENV_DOCS["SPOOLCTL_DB"],
        "type": "path",
        "required": False,
        "default": "./.spoolctl/queue.db",
        "minimum": 1,
        "maximum": MAX_PATH_CHARS,
        "unbounded": False,
        "shadowed_by": "--db",
        "consumed_by": DB_VERBS,
        "malformed_expectations": {"bad_path": EXPECT_INVALID},
    },
    "SPOOLCTL_TEST_HEARTBEAT_INTERVAL": {
        "description": ENV_DOCS["SPOOLCTL_TEST_HEARTBEAT_INTERVAL"],
        "type": "float",
        "required": False,
        "default": HEARTBEAT_INTERVAL,
        "minimum": 0,
        "maximum": MAX_WAIT_SECONDS,
        "unbounded": False,
        "grammar": "finite float > 0",
        "shadowed_by": None,
        "consumed_by": ["work"],
        "malformed_expectations": {"bad_type": EXPECT_INVALID},
    },
    "SPOOLCTL_TEST_REAP_THRESHOLD": {
        "description": ENV_DOCS["SPOOLCTL_TEST_REAP_THRESHOLD"],
        "type": "float",
        "required": False,
        "default": REAP_THRESHOLD,
        "minimum": 0,
        "maximum": MAX_WAIT_SECONDS,
        "unbounded": False,
        "grammar": "finite float > 0",
        "shadowed_by": None,
        "consumed_by": ["work"],
        "malformed_expectations": {"bad_type": EXPECT_INVALID},
    },
}


def _default_malformed_expectations(value_required: bool, ftype: str) -> dict[str, dict[str, Any]]:
    expectations: dict[str, dict[str, Any]] = {}
    if value_required:
        expectations["missing_value"] = EXPECT_INVALID
        expectations["value_is_another_flag"] = EXPECT_INVALID
    if ftype in {"duration", "enum", "enum_csv", "float", "integer", "key_value", "queue", "timestamp"}:
        expectations["bad_type"] = EXPECT_INVALID
    if ftype in {"duration", "float", "integer", "timestamp"}:
        expectations["out_of_range"] = EXPECT_INVALID
    return expectations


def _action_base_type(action: argparse.Action) -> str:
    if isinstance(action, argparse._StoreTrueAction):
        return "boolean"
    if callable(action.type):
        if action.type.__name__ == "int":
            return "integer"
        if action.type.__name__ == "float":
            return "float"
    return "string"


def _canonical_flag(action: argparse.Action) -> str:
    long_options = [s for s in action.option_strings if s.startswith("--")]
    return long_options[0] if long_options else max(action.option_strings, key=len)


def _with_flag_contract(verb_name: str, action: argparse.Action, flag: str) -> dict[str, Any]:
    value_required = not isinstance(action, argparse._StoreTrueAction)
    repeatable = isinstance(action, argparse._AppendAction)
    ftype = _action_base_type(action)
    entry: dict[str, Any] = {
        "aliases": sorted(action.option_strings),
        "choices": sorted(action.choices) if action.choices else None,
        "default": action.default,
        "flag": flag,
        "malformed_expectations": _default_malformed_expectations(value_required, ftype),
        "name": flag,
        "repeatable": repeatable,
        "required": bool(getattr(action, "required", False)),
        "type": ftype,
        "unbounded": False,
        "value_required": value_required,
    }
    if not value_required:
        entry["default"] = False
        entry["minimum"] = None
        entry["maximum"] = None
    override = {**FLAG_CONTRACTS.get(flag, {}), **VERB_FLAG_CONTRACTS.get((verb_name, flag), {})}
    if override:
        entry.update(override)
        entry["malformed_expectations"] = {
            **_default_malformed_expectations(value_required, entry["type"]),
            **override.get("malformed_expectations", {}),
        }
        if "choices" not in override and action.choices:
            entry["choices"] = sorted(action.choices)
        entry["repeatable"] = override.get("repeatable", repeatable)
    if entry["type"] == "integer" and "minimum" not in entry:
        entry.update({"minimum": SQLITE_INT64_MIN, "maximum": SQLITE_INT64_MAX})
    if entry["type"] == "float" and "minimum" not in entry:
        entry.update({"minimum": 0, "maximum": 1e308, "grammar": "finite float > 0"})
    if entry["type"] == "string" and "minimum" not in entry:
        entry.update({
            "minimum": 0,
            "maximum": None,
            "unbounded": True,
            "unbounded_reason": "no explicit CLI length cap",
        })
    if entry["type"] in {"enum", "enum_csv"} and entry.get("choices") is None and action.choices:
        entry["choices"] = sorted(action.choices)
    return entry


def _nargs_name(nargs: Any) -> str:
    if nargs in (None, 1):
        return "1"
    if nargs == "+":
        return "+"
    if nargs == "*":
        return "*"
    if nargs == argparse.REMAINDER:
        return "remainder"
    return str(nargs)


def _describe_positional(action: argparse.Action) -> dict[str, Any]:
    nargs = _nargs_name(action.nargs)
    entry: dict[str, Any] = {
        "choices": sorted(action.choices) if action.choices else None,
        "malformed_expectations": {
            "missing_value": EXPECT_MISSING,
            "bad_type": EXPECT_INVALID,
            "out_of_range": EXPECT_INVALID,
        },
        "name": action.dest,
        "nargs": nargs,
        "repeatable": nargs in {"+", "*", "remainder"},
        "required": nargs in {"1", "+"},
        "type": "string",
        "unbounded": False,
    }
    entry.update(POSITIONAL_CONTRACTS.get(action.dest, {}))
    if entry["type"] == "integer":
        entry.setdefault("minimum", 1)
        entry.setdefault("maximum", SQLITE_INT64_MAX)
    if entry["type"] == "string":
        entry.setdefault("minimum", 0)
        entry.setdefault("maximum", None)
        entry.setdefault("unbounded", True)
        entry.setdefault("unbounded_reason", "no explicit CLI length cap")
    return entry


def _describe_verb(name: str, sub: argparse.ArgumentParser) -> dict[str, Any]:
    flags = []
    args = []
    for action in sub._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if action.option_strings:
            flag = _canonical_flag(action)
            flags.append(_with_flag_contract(name, action, flag))
        else:
            args.append(_describe_positional(action))
    modes = VERB_OUTPUT_MODES.get(name, ["envelope", "text"])
    traits = VERB_TRAITS[name]
    description = {
        "args": args,
        "data_schema": VERB_SUMMARIES[name]["data_schema"],
        "description": VERB_SUMMARIES[name]["summary"],
        "destructive": traits["destructive"],
        "examples": VERB_EXAMPLES[name],
        "exit_codes": sorted(EXIT_CODES),
        "flags": sorted(flags, key=lambda f: f["flag"]),
        "idempotent": traits["idempotent"],
        "json": {
            "flag": "--json",
            "mode": "envelope",
            "schema_ref": f"spoolctl schema --json --verb {name}",
        },
        "mutates": traits["mutates"],
        "mutually_exclusive": VERB_MUTEX.get(name, []),
        "output_modes": modes,
        "output_schema": schemas.VERB_SCHEMAS.get(name),
        "positionals": args,
        "probe_hints": PROBE_HINTS,
        "schema_ref": f"spoolctl schema --json --verb {name}",
        "stdin": "none",
        "summary": VERB_SUMMARIES[name]["summary"],
        "text": {
            "mode": "text",
            "contract": "human-readable stdout on success; diagnostics on stderr for errors",
        },
    }
    if name == "events":
        description["frames"] = {
            "enter_with": ["--follow", "--json"],
            "record_schema": "#/streams/events_follow",
            "frame_discriminator": (
                "data frames carry integer id; control frames carry object"
                " control and never carry id"
            ),
            "control_frames": ["end", "error"],
            "control_shape": {
                "control": {
                    "type": "end|error",
                    "reason": "max_events|idle_timeout|sqlite_error",
                }
            },
            "cursor": {
                "field": "id",
                "flag": "--since-id",
                "aliases": ["--since-cursor"],
            },
            "delivery": "best-effort-tail-over-replayable-ledger",
            "defaults": {
                "follow_start": "high-water mark unless --since-id/--since-cursor is supplied",
                "one_shot_start": "beginning unless --since-id/--since-cursor is supplied",
            },
            "termination": {
                "signals": ["SIGINT", "SIGTERM", "EPIPE"],
                "max_events": "emits final end control frame with reason max_events",
                "idle_timeout": "emits final end control frame with reason idle_timeout",
            },
            "ordering": (
                "end appears exactly once and is final for graceful bounded"
                " termination; signal/EPIPE termination may omit it"
            ),
        }
        description["raw"] = {
            "deprecated": True,
            "replaced_by": "frames",
        }
        description["frames_mode"] = description["frames"]
        description["output_modes"] = ["envelope", "frames", "text"]
        description["probe_hints"] = {
            **description["probe_hints"],
            "frames_success": [
                "seed event ledger and run events --follow --json --since-id 0 --max-events 1",
                "run events --follow --json --idle-timeout 0.1 on an empty ledger",
            ],
        }
        description["raw_legacy"] = {
            "delivery_class": "best-effort-tail over replayable-from-cursor ledger",
            "mode": "ndjson",
            "record": "contract v1 bare event records; contract v2 uses frames with control frames",
            "stream": "events_follow_data",
            "when": "not available in contract_version 2",
        }
        description["since_cursor_alias"] = "--since-cursor"
    if name == "prune":
        description["destructive"] = True
        description["safety"] = {
            "confirmation_flag": "--yes",
            "dry_run_flag": "--dry-run",
            "refusal_code": "SAFETY_BLOCK",
            "refusal_exit_code": EXIT_SAFETY,
            "invalid_combinations": [
                {"flags": ["--yes", "--dry-run"], "code": "INVALID_INPUT"}
            ],
        }
    if name == "cancel":
        description["destructive"] = "only with --running"
        description["interrupts_process"] = "only with --running"
        description["safety"] = {
            "confirmation_flag": "--yes",
            "requires": ["--running", "--yes"],
            "refusal_code": "SAFETY_BLOCK",
            "refusal_exit_code": EXIT_SAFETY,
            "queued_cancel": {
                "destructive": False,
                "interrupts_process": False,
            },
        }
    if name == "retry":
        description["safety"] = {
            "force_flag": "--force",
            "force_required_for": "running_job",
            "refusal_code": "SAFETY_BLOCK",
            "refusal_exit_code": EXIT_SAFETY,
            "also_requires_yes": False,
        }
    if name == "output":
        description["raw"] = {
            "enter_with": ["--raw"],
            "incompatible_with": ["--json"],
            "requires": {"--stream": ["stdout", "stderr"]},
            "delivery_class": "raw captured bytes",
            "failure_stdout": "no diagnostic text or JSON",
        }
    return description


CONTRACT_POLICY = (
    "contract_version 2 is the v0.4.5 pre-release hardening contract; it"
    " intentionally breaks v1 quirks by refusing unsafe destructive operations,"
    " rejecting inert or abbreviated flags, enforcing active idempotency"
    " execution-payload conflicts, making malformed inputs total and"
    " structured, and declaring envelope, frames, raw, and text modes."
    " No contract_version 1 compatibility shim is provided before public release."
)


SCHEDULING_CAPABILITIES = {
    "duration_grammar": "<number>[s|m|h|d] or bare seconds; lowercase units only",
    "at_timestamp": (
        "epoch seconds or ISO-8601; naive ISO timestamps use the local process timezone"
    ),
    "finite_numeric_inputs": ["--after", "--at epoch seconds"],
    "priority": {
        "default": 0,
        "max": PRIORITY_MAX,
        "min": PRIORITY_MIN,
        "ordering": "higher priority first, then next_run_at ascending, then id ascending",
    },
    "queue": {
        "default": "default",
        "grammar": "1-64 chars matching ^[A-Za-z0-9][A-Za-z0-9._-]*$",
        "single_lane_worker": True,
    },
    "slots": {
        "claimed_false": (
            "work --once --json {claimed:false} means no job runnable by this worker"
            " right now, including a full slot ceiling"
        ),
        "default_ceiling": None,
        "fleet_global": True,
        "opt_in": True,
        "scope": "served lane",
    },
    "scheduled": {
        "counts_included_in": "counts.queued",
        "includes": ["user-delayed rows", "retry/reap backoff rows"],
        "predicate": "state='queued' AND next_run_at > now",
    },
    "drain": {
        "holds_for": [
            "running rows in the served lane",
            "due queued rows in the served lane",
            "future queued retry/reap backoff rows in the served lane (attempts > 0)",
        ],
        "scope": "served lane",
        "skips": "future queued never-run user-delayed rows (attempts = 0)",
    },
}


EXECUTION_CAPABILITIES = {
    "cwd": {
        "flag": "--cwd DIR",
        "default": None,
        "resolution": "resolved to os.path.abspath at submit time; symlinks are not realpath-collapsed",
        "runtime_failure": "missing or non-directory cwd is a job-owned spawn failure governed by --max-retries",
    },
    "env_overrides": {
        "flag": "--env K=V",
        "repeatable": True,
        "semantics": "augment worker environment; overrides are layered over os.environ at run time",
        "values_stored_plaintext": True,
        "values_in_add_or_list": False,
        "values_in_show": True,
        "grammar": "split on first '='; key non-empty; NUL rejected in key and value; repeated keys last-win",
    },
    "retry_model": {
        "attempts": "recorded unsuccessful executions: job-owned failures plus worker crashes",
        "job_owned_failures": "nonzero exit, timeout, or spawn failure; count is attempts - crashes",
        "worker_crashes": "confirmed-dead worker reaps; count stored in crashes",
        "max_retries": "bounds job-owned failure requeues only",
        "max_crashes": {
            "default": None,
            "meaning": "worker crashes tolerated before dead-lettering; null is unbounded",
            "zero": "first crash dead-letters",
            "one": "first crash requeues, second crash dead-letters",
        },
        "backoff": "job-owned failures and crash redeliveries both use backoff_seconds(attempts)",
        "wait_drain": "with unbounded crashes, a deterministically crashing job can keep wait blocking and work --drain unsettled until canceled or bounded",
    },
}


ROBOT_DOC_SECTIONS = [
    {
        "title": "Discover the contract",
        "bullets": [
            "spoolctl capabilities --json",
            "spoolctl schema --json",
            "spoolctl brief --json",
        ],
    },
    {
        "title": "Submit and observe work",
        "bullets": [
            "spoolctl add --json --key run-1 --tag owner=agent -- true",
            "spoolctl work --json --once",
            "spoolctl wait --json 1",
            "spoolctl output --json 1 --stream stdout",
        ],
    },
    {
        "title": "Handle conflicts and safety",
        "bullets": [
            "Reuse --key only with the same execution payload.",
            "Use prune --dry-run before prune --yes.",
            "Use cancel --running --yes only when process interruption is intended.",
            "Use retry --force only for a running-job recovery override.",
        ],
    },
    {
        "title": "Stream and page safely",
        "bullets": [
            "spoolctl events --json --limit 100",
            "spoolctl events --follow --json --since-id 0 --max-events 1",
            "spoolctl events --follow --json --idle-timeout 0.1",
            "spoolctl list --json --limit 50",
        ],
    },
]


def _robot_docs_text() -> tuple[str, int]:
    lines = ["spoolctl robot-docs guide"]
    for section in ROBOT_DOC_SECTIONS:
        lines.append("")
        lines.append(section["title"])
        for bullet in section["bullets"]:
            lines.append(f"- {bullet}")
    text = "\n".join(lines)
    return text, schemas.approx_tokens(text)



def build_robot_docs() -> tuple[dict[str, Any], str]:
    text, tokens = _robot_docs_text()
    return {
        "approx_tokens": tokens,
        "sections": ROBOT_DOC_SECTIONS,
        "text": text,
    }, text


def build_brief() -> tuple[dict[str, Any], str]:
    text, tokens = schemas.build_brief(VERB_SUMMARIES, EXIT_CODES, JOB_STATES, ENV_DOCS)
    return {
        "approx_tokens": tokens,
        "budget_tokens": schemas.BRIEF_BUDGET_TOKENS,
        "text": text,
    }, text


def build_schema(schema_verb: str | None) -> tuple[dict[str, Any], str]:
    verb_names = sorted(schemas.VERB_SCHEMAS)
    if schema_verb is None:
        selected = {name: schemas.VERB_SCHEMAS[name] for name in verb_names}
    else:
        if schema_verb not in schemas.VERB_SCHEMAS:
            suggestion = _suggest(schema_verb, verb_names)
            raise CliError(
                "INVALID_INPUT",
                f"unknown schema verb: {schema_verb!r}",
                f"valid verbs: {', '.join(verb_names)}",
                did_you_mean=suggestion,
            )
        selected = {schema_verb: schemas.VERB_SCHEMAS[schema_verb]}
    data = {
        "dialect": schemas.DIALECT,
        "envelope_schema": schemas.ENVELOPE_SCHEMA,
        "streams": schemas.STREAM_SCHEMAS,
        "verbs": selected,
    }
    lines = [
        "spoolctl JSON Schemas:",
        "verbs: " + ", ".join(verb_names),
        "run with --json for envelope_schema, verbs, and streams",
    ]
    return data, "\n".join(lines)


def build_capabilities(
    parser_verbs: Mapping[str, argparse.ArgumentParser],
) -> tuple[dict[str, Any], str]:
    verbs = {name: _describe_verb(name, sub) for name, sub in sorted(parser_verbs.items())}
    exit_codes = {
        str(code): dict(sorted(info.items()))
        for code, info in sorted(EXIT_CODES.items())
    }
    code_registry = {code: dict(entry) for code, entry in sorted(CODE_REGISTRY.items())}
    data = {
        "attempt_states": sorted(ATTEMPT_STATES),
        "code_registry": code_registry,
        "config": {
            "supported": False,
            "reason": "no config file surface in v0.4.5",
        },
        "contract_policy": CONTRACT_POLICY,
        "contract_version": CONTRACT_VERSION,
        "env": ENV_DOCS,
        "env_vars": ENV_VAR_CONTRACTS,
        "error_codes": sorted(ERROR_CODES),
        "events": sorted(JOB_EVENT_TYPES),
        "execution": EXECUTION_CAPABILITIES,
        "exit_codes": exit_codes,
        "failure_reasons": list(FAILURE_REASONS),
        "features": FEATURES,
        "global_flags": {
            "flags": GLOBAL_FLAGS,
            "db_scope": "--db is verb-local on database-reading verbs, not global",
        },
        "job_states": sorted(JOB_STATES),
        "limits": LIMITS,
        "output_modes": OUTPUT_MODES,
        "probe_vocabularies": PROBE_VOCABULARIES,
        "robot_docs_uri": "spoolctl robot-docs guide",
        "scheduling": SCHEDULING_CAPABILITIES,
        "schemas_uri": "spoolctl schema --json",
        "tool_name": "spoolctl",
        "tool_version": TOOL_VERSION,
        "totality": TOTALITY_CONTRACT,
        "verbs": verbs,
    }
    lines = [f"spoolctl contract v{CONTRACT_VERSION}"]
    for name, verb in verbs.items():
        lines.append(f"  {name}: {verb['summary']}")
    lines.append("run with --json for the full machine-readable contract")
    return data, "\n".join(lines)
