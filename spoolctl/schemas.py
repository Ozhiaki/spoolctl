"""JSON Schema definitions for spoolctl's machine contract.

The schemas are runtime data exported by the future `schema` verb. Validation
stays test-only so the CLI keeps zero runtime dependencies.
"""

from __future__ import annotations

import math
from typing import Any

from spoolctl.models import (
    ATTEMPT_STATES,
    CODE_REGISTRY,
    EXIT_CODES,
    FAILURE_REASONS,
    JOB_EVENT_TYPES,
    JOB_STATES,
    ERROR_CODES,
    WARNING_CODES,
)

DIALECT = "https://json-schema.org/draft/2020-12/schema"
BRIEF_BUDGET_TOKENS = 700

NULLABLE_STRING = {"type": ["string", "null"]}
NULLABLE_INTEGER = {"type": ["integer", "null"]}
NULLABLE_NUMBER = {"type": ["number", "null"]}
FAILURE_REASON_SCHEMA = {
    "type": ["string", "null"],
    "enum": [*FAILURE_REASONS, None],
}


def array_of(item_schema: dict) -> dict:
    return {"type": "array", "items": item_schema}


def obj(properties: dict, required: list[str] | None = None, additional=False) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else sorted(properties),
        "additionalProperties": additional,
    }


ERROR_SCHEMA = obj({
    "code": {"type": "string", "enum": list(ERROR_CODES)},
    "message": {"type": "string"},
    "remediation": {"type": "string"},
    "exit_code": {"type": "integer"},
    "did_you_mean": NULLABLE_STRING,
}, required=["code", "message", "remediation", "exit_code"])

WARNING_SCHEMA = obj({
    "code": {"type": "string", "enum": list(WARNING_CODES)},
    "message": {"type": "string"},
}, required=["code", "message"])

CODE_REGISTRY_ENTRY_SCHEMA = obj({
    "appears_in": array_of({"type": "string", "enum": ["errors", "warnings"]}),
    "summary": {"type": "string"},
    "exit_code": {"type": "integer"},
    "retryable": {"type": ["boolean", "null"]},
    "example": {"type": "string"},
}, required=["appears_in", "summary", "retryable"], additional=False)

PAGINATION_SCHEMA = obj({
    "cursor": {"type": "integer"},
    "first_id": NULLABLE_INTEGER,
    "limit": {"type": "integer"},
    "scan_limit": {"type": "integer"},
    "scanned": {"type": "integer"},
    "truncated": {"type": "boolean"},
}, required=["cursor", "first_id"])

WAIT_META_SCHEMA = obj({
    "waited_ms": {"type": "integer"},
    "reason": {"type": "string", "enum": ["records_available", "timeout"]},
})

ENVELOPE_SCHEMA = obj({
    "ok": {"type": "boolean"},
    "tool_version": {"type": "string"},
    "data": {},
    "meta": obj({
        "request_id": {"type": "string"},
        "ts_iso": {"type": "string"},
        "elapsed_ms": {"type": "integer"},
        "contract_version": {"type": "string"},
        "data_hash": {"type": "string"},
        "pagination": PAGINATION_SCHEMA,
        "wait": WAIT_META_SCHEMA,
    }, required=["request_id", "ts_iso", "elapsed_ms", "contract_version", "data_hash"]),
    "warnings": array_of(WARNING_SCHEMA),
    "commands": array_of({"type": "string"}),
    "errors": array_of(ERROR_SCHEMA),
})

JOB_METADATA_PROPS = {
    "idempotency_key": NULLABLE_STRING,
    "tags": {"type": "object", "additionalProperties": {"type": "string"}},
    "note": NULLABLE_STRING,
}

LIST_JOB_SCHEMA = obj({
    "argv": array_of({"type": "string"}),
    "attempts": {"type": "integer"},
    "crashes": {"type": "integer"},
    "created_at": {"type": "number"},
    "cwd": NULLABLE_STRING,
    "finished_at": NULLABLE_NUMBER,
    "id": {"type": "integer"},
    "last_error": NULLABLE_STRING,
    "last_exit_code": NULLABLE_INTEGER,
    "max_retries": {"type": "integer"},
    "next_run_at": {"type": "number"},
    "priority": {"type": "integer"},
    "queue": {"type": "string"},
    "started_at": NULLABLE_NUMBER,
    "state": {"type": "string", "enum": list(JOB_STATES)},
    "timeout_seconds": {"type": "integer"},
    **JOB_METADATA_PROPS,
})

SHOW_JOB_SCHEMA = obj({
    **LIST_JOB_SCHEMA["properties"],
    "env": {"type": "object", "additionalProperties": {"type": "string"}},
    "heartbeat_at": NULLABLE_NUMBER,
    "locked_at": NULLABLE_NUMBER,
    "locked_by": NULLABLE_STRING,
    "locked_pid": NULLABLE_INTEGER,
    "last_failure_reason": FAILURE_REASON_SCHEMA,
    "max_crashes": NULLABLE_INTEGER,
})

ATTEMPT_SCHEMA = obj({
    "attempt_no": {"type": "integer"},
    "error": NULLABLE_STRING,
    "exit_code": NULLABLE_INTEGER,
    "failure_reason": FAILURE_REASON_SCHEMA,
    "finished_at": NULLABLE_NUMBER,
    "started_at": {"type": "number"},
    "state": {"type": "string", "enum": list(ATTEMPT_STATES)},
    "stderr_path": {"type": "string"},
    "stdout_path": {"type": "string"},
    "worker_id": {"type": "string"},
    "worker_pid": {"type": "integer"},
})

EVENT_RECORD_SCHEMA = obj({
    "id": {"type": "integer"},
    "job_id": {"type": "integer"},
    "at": {"type": "number"},
    "event": {"type": "string", "enum": list(JOB_EVENT_TYPES)},
    "worker_id": NULLABLE_STRING,
    "detail": NULLABLE_STRING,
})

EVENT_CONTROL_FRAME_SCHEMA = obj({
    "control": obj({
        "exit_code": {"type": "integer"},
        "message": {"type": "string"},
        "reason": {"type": "string", "enum": ["max_events", "idle_timeout", "sqlite_error"]},
        "type": {"type": "string", "enum": ["end", "error"]},
    }, required=["type", "reason"]),
})

SHOW_EVENT_SCHEMA = obj({
    "at": {"type": "number"},
    "event": {"type": "string", "enum": list(JOB_EVENT_TYPES)},
    "worker_id": NULLABLE_STRING,
    "detail": NULLABLE_STRING,
})

STREAM_SCHEMA = obj({
    "path": {"type": "string"},
    "preview": {"type": "string"},
    "preview_truncated": {"type": "boolean"},
    "size_bytes": {"type": "integer"},
})

DIFFERENCE_SCHEMA = obj({
    "existing": {},
    "submitted": {},
})

IDEMPOTENCY_RESULT_SCHEMA = obj({
    "key": {"type": "string"},
    "metadata_differs": {"type": "boolean"},
    "metadata_differences": {
        "type": "object",
        "additionalProperties": DIFFERENCE_SCHEMA,
    },
})

EXPECTATION_SCHEMA = obj({
    "code": {"type": "string"},
    "exit_code": {"type": "integer"},
})

CAP_LIMIT_SCHEMA = obj({
    "charset": {"type": "string"},
    "grammar": {"type": "string"},
    "maximum": {"type": ["number", "null"]},
    "minimum": {"type": ["number", "null"]},
    "type": {"type": "string"},
    "unbounded": {"type": "boolean"},
    "unbounded_reason": {"type": "string"},
}, required=["type", "unbounded"], additional=True)

CAP_FLAG_SCHEMA = obj({
    "aliases": array_of({"type": "string"}),
    "charset": {"type": "string"},
    "choices": {"type": ["array", "null"], "items": {"type": "string"}},
    "default": {},
    "flag": {"type": "string"},
    "grammar": {"type": "string"},
    "key_maximum": {"type": "integer"},
    "malformed_expectations": {
        "type": "object",
        "additionalProperties": EXPECTATION_SCHEMA,
    },
    "maximum": {"type": ["number", "null"]},
    "maximum_items": {"type": "integer"},
    "minimum": {"type": ["number", "null"]},
    "name": {"type": "string"},
    "repeatable": {"type": "boolean"},
    "required": {"type": "boolean"},
    "type": {"type": "string"},
    "unbounded": {"type": "boolean"},
    "unbounded_reason": {"type": "string"},
    "value_maximum": {"type": "integer"},
    "value_required": {"type": "boolean"},
}, required=[
    "aliases",
    "choices",
    "default",
    "flag",
    "malformed_expectations",
    "name",
    "repeatable",
    "required",
    "type",
    "unbounded",
    "value_required",
], additional=True)

CAP_ARG_SCHEMA = obj({
    "choices": {"type": ["array", "null"], "items": {"type": "string"}},
    "malformed_expectations": {
        "type": "object",
        "additionalProperties": EXPECTATION_SCHEMA,
    },
    "maximum": {"type": ["number", "null"]},
    "minimum": {"type": ["number", "null"]},
    "name": {"type": "string"},
    "nargs": {"type": "string", "enum": ["1", "+", "*", "?", "remainder"]},
    "repeatable": {"type": "boolean"},
    "required": {"type": "boolean"},
    "type": {"type": "string"},
    "unbounded": {"type": "boolean"},
    "unbounded_reason": {"type": "string"},
}, required=[
    "choices",
    "malformed_expectations",
    "name",
    "nargs",
    "repeatable",
    "required",
    "type",
    "unbounded",
], additional=True)

CAP_VERB_SCHEMA = obj({
    "args": array_of(CAP_ARG_SCHEMA),
    "data_schema": {"type": "string"},
    "description": {"type": "string"},
    "destructive": {},
    "examples": array_of(array_of({"type": "string"})),
    "exit_codes": array_of({"type": "integer"}),
    "flags": array_of(CAP_FLAG_SCHEMA),
    "frames": {"type": "object", "additionalProperties": {}},
    "frames_mode": {"type": "object", "additionalProperties": {}},
    "idempotent": {},
    "json": {"type": "object", "additionalProperties": {}},
    "mutates": {"type": "boolean"},
    "mutually_exclusive": array_of(array_of({"type": "string"})),
    "output_modes": array_of({"type": "string", "enum": ["envelope", "frames", "raw", "text"]}),
    "output_schema": {},
    "positionals": array_of(CAP_ARG_SCHEMA),
    "probe_hints": {"type": "object", "additionalProperties": {}},
    "raw": {"type": "object", "additionalProperties": {}},
    "safety": {"type": "object", "additionalProperties": {}},
    "schema_ref": {"type": "string"},
    "since_cursor_alias": {"type": "string"},
    "stdin": {"type": "string"},
    "summary": {"type": "string"},
    "text": {"type": "object", "additionalProperties": {}},
}, required=[
    "args",
    "data_schema",
    "description",
    "destructive",
    "examples",
    "exit_codes",
    "flags",
    "idempotent",
    "json",
    "mutates",
    "mutually_exclusive",
    "output_modes",
    "output_schema",
    "positionals",
    "probe_hints",
    "schema_ref",
    "stdin",
    "summary",
    "text",
], additional=True)

ENV_VAR_SCHEMA = obj({
    "consumed_by": array_of({"type": "string"}),
    "default": {},
    "description": {"type": "string"},
    "grammar": {"type": "string"},
    "malformed_expectations": {
        "type": "object",
        "additionalProperties": EXPECTATION_SCHEMA,
    },
    "maximum": {"type": ["number", "null"]},
    "minimum": {"type": ["number", "null"]},
    "required": {"type": "boolean"},
    "shadowed_by": NULLABLE_STRING,
    "type": {"type": "string"},
    "unbounded": {"type": "boolean"},
    "unbounded_reason": {"type": "string"},
}, required=[
    "consumed_by",
    "default",
    "description",
    "malformed_expectations",
    "required",
    "shadowed_by",
    "type",
    "unbounded",
], additional=True)

VERB_SCHEMAS = {
    "add": obj({
        "cwd": NULLABLE_STRING,
        "deduplicated": {"type": "boolean"},
        "env_keys": array_of({"type": "string"}),
        "idempotency": IDEMPOTENCY_RESULT_SCHEMA,
        "job_id": {"type": "integer"},
        "next_run_at": {"type": "number"},
        "priority": {"type": "integer"},
        "queue": {"type": "string"},
        "state": {"type": "string", "enum": ["queued", "running"]},
    }, required=[
        "cwd",
        "deduplicated",
        "env_keys",
        "job_id",
        "next_run_at",
        "priority",
        "queue",
        "state",
    ]),
    "work": {
        "oneOf": [
            obj({"claimed": {"type": "boolean", "const": False}}),
            obj({
                "claimed": {"type": "boolean", "const": True},
                "attempt_no": {"type": "integer"},
                "job_id": {"type": "integer"},
                "job_state": {"type": ["string", "null"]},
                "result": {"type": "string", "enum": ["succeeded", "failed", "timed_out"]},
            }),
            obj({"drained": {"type": "boolean"}, "executed": {"type": "integer"}}),
            obj({"stopped": {"type": "boolean", "const": True}}),
        ]
    },
    "wait": obj({
        "all_succeeded": {"type": "boolean"},
        "jobs": {"type": "object", "additionalProperties": obj({
            "attempts": {"type": "integer"},
            "last_error": NULLABLE_STRING,
            "last_exit_code": NULLABLE_INTEGER,
            "state": {"type": "string", "enum": list(JOB_STATES)},
        })},
    }),
    "status": obj({
        "counts": obj({state: {"type": "integer"} for state in sorted(JOB_STATES)}),
        "scheduled": {"type": "integer"},
        "queues": {
            "type": "object",
            "additionalProperties": obj({
                "counts": obj({state: {"type": "integer"} for state in sorted(JOB_STATES)}),
                "scheduled": {"type": "integer"},
            }),
        },
        "recent_dead": array_of(obj({
            "attempts": {"type": "integer"},
            "command": {"type": "string"},
            "crashes": {"type": "integer"},
            "finished_at": NULLABLE_NUMBER,
            "id": {"type": "integer"},
            "last_error": NULLABLE_STRING,
            "stderr_path": NULLABLE_STRING,
            "stdout_path": NULLABLE_STRING,
        })),
    }),
    "list": obj({"count": {"type": "integer"}, "jobs": array_of(LIST_JOB_SCHEMA)}),
    "show": obj({
        "attempts": array_of(ATTEMPT_SCHEMA),
        "events": array_of(SHOW_EVENT_SCHEMA),
        "job": SHOW_JOB_SCHEMA,
    }),
    "retry": obj({"job_id": {"type": "integer"}, "state": {"type": "string", "const": "queued"}}),
    "cancel": obj({
        "job_id": {"type": "integer"},
        "state": {"type": "string", "const": "canceled"},
        "was_running": {"type": "boolean"},
    }),
    "prune": obj({
        "actual": {"type": "boolean"},
        "deleted_attempts": {"type": "integer"},
        "deleted_events": {"type": "integer"},
        "deleted_jobs": {"type": "integer"},
        "dry_run": {"type": "boolean"},
        "freed_bytes": {"type": "integer"},
        "irreversible": {"type": "boolean"},
        "matched": {"type": "integer"},
    }, required=[
        "actual",
        "deleted_attempts",
        "deleted_events",
        "deleted_jobs",
        "dry_run",
        "freed_bytes",
        "matched",
    ]),
    "output": {
        "oneOf": [
            obj({"attempts": array_of({})}),
            obj({
                "attempt_no": {"type": "integer"},
                "attempt_state": {"type": "string", "enum": list(ATTEMPT_STATES)},
                "attempts_total": {"type": "integer"},
                "job_id": {"type": "integer"},
                "streams": {"type": "object", "additionalProperties": STREAM_SCHEMA},
            }),
        ]
    },
    "events": obj({
        "count": {"type": "integer"},
        "events": array_of(EVENT_RECORD_SCHEMA),
    }),
    "config-show": obj({
        "config_path": NULLABLE_STRING,
        "config_exists": {"type": "boolean"},
        "config_valid": {"type": "boolean"},
        "values": obj({"db_path": {"type": "string"}}),
        "sources": obj({
            "db_path": {
                "type": "string",
                "enum": ["flag", "environment", "project_config", "default"],
            },
        }),
        "precedence": array_of({
            "type": "string",
            "enum": ["flag", "environment", "project_config", "default"],
        }),
        "ignored_keys": array_of({"type": "string"}),
    }),
    "config-validate": obj({
        "config_path": {"type": "string"},
        "exists": {"type": "boolean"},
        "valid": {"type": "boolean"},
        "format": {"type": "string", "const": "json"},
        "schema_version": {"type": "integer", "const": 1},
        "recognized_keys": array_of({"type": "string", "enum": ["db_path"]}),
        "unknown_keys": array_of({"type": "string"}),
    }),
    "doctor": obj({
        "ready": {"type": "boolean"},
        "summary": obj({
            "passed": {"type": "integer"},
            "warnings": {"type": "integer"},
            "failed": {"type": "integer"},
            "skipped": {"type": "integer"},
        }),
        "config": obj({
            "config_path": NULLABLE_STRING,
            "config_exists": {"type": "boolean"},
            "config_valid": {"type": "boolean"},
            "db_path": NULLABLE_STRING,
            "db_source": {
                "type": ["string", "null"],
                "enum": ["flag", "environment", "project_config", "default", None],
            },
        }),
        "checks": array_of(obj({
            "id": {"type": "string"},
            "status": {"type": "string", "enum": ["pass", "warn", "fail", "skip"]},
            "message": {"type": "string"},
            "remediation": NULLABLE_STRING,
            "blocked_by": NULLABLE_STRING,
        })),
        "versions": obj({
            "tool_version": {"type": "string"},
            "contract_version": {"type": "string"},
            "schema_version": {"type": "integer"},
        }),
    }),
    "brief": obj({
        "approx_tokens": {"type": "integer"},
        "budget_tokens": {"type": "integer", "const": BRIEF_BUDGET_TOKENS},
        "text": {"type": "string"},
    }),
    "robot-docs": obj({
        "approx_tokens": {"type": "integer"},
        "sections": array_of(obj({
            "bullets": array_of({"type": "string"}),
            "title": {"type": "string"},
        })),
        "text": {"type": "string"},
    }),
    "schema": obj({
        "dialect": {"type": "string", "const": DIALECT},
        "envelope_schema": {},
        "streams": {"type": "object", "additionalProperties": {}},
        "verbs": {"type": "object", "additionalProperties": {}},
    }),
    "capabilities": obj({
        "attempt_states": array_of({"type": "string"}),
        "config": obj({
            "supported": {"type": "boolean"},
            "path": {"type": "string"},
            "format": {"type": "string", "const": "json"},
            "schema_version": {"type": "integer"},
            "precedence": array_of({
                "type": "string",
                "enum": ["flag", "environment", "project_config", "default"],
            }),
            "keys": array_of({"type": "string"}),
        }, required=["supported"]),
        "contract_policy": {"type": "string"},
        "contract_version": {"type": "string"},
        "code_registry": {
            "type": "object",
            "properties": {code: CODE_REGISTRY_ENTRY_SCHEMA for code in CODE_REGISTRY},
            "required": sorted(CODE_REGISTRY),
            "additionalProperties": False,
        },
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
        "env_vars": {"type": "object", "additionalProperties": ENV_VAR_SCHEMA},
        "error_codes": array_of({"type": "string", "enum": list(ERROR_CODES)}),
        "events": array_of({"type": "string"}),
        "execution": {"type": "object", "additionalProperties": {}},
        "exit_codes": {
            "type": "object",
            "additionalProperties": obj({
                "meaning": {"type": "string"},
                "note": {"type": "string"},
                "retryable": {"type": ["boolean", "null"]},
            }, required=["meaning", "retryable"]),
        },
        "failure_reasons": array_of({"type": "string", "enum": list(FAILURE_REASONS)}),
        "features": array_of({"type": "string"}),
        "global_flags": obj({
            "db_scope": {"type": "string"},
            "flags": array_of(CAP_FLAG_SCHEMA),
        }),
        "job_states": array_of({"type": "string"}),
        "limits": {"type": "object", "additionalProperties": CAP_LIMIT_SCHEMA},
        "output_modes": array_of({"type": "string", "enum": ["envelope", "frames", "raw", "text"]}),
        "probe_vocabularies": {"type": "object", "additionalProperties": {}},
        "robot_docs_uri": NULLABLE_STRING,
        "scheduling": {"type": "object", "additionalProperties": {}},
        "schemas_uri": {"type": "string"},
        "tool_name": {"type": "string", "const": "spoolctl"},
        "tool_version": {"type": "string"},
        "totality": {"type": "object", "additionalProperties": {}},
        "verbs": {"type": "object", "additionalProperties": CAP_VERB_SCHEMA},
    }),
}

STREAM_SCHEMAS = {
    "events_follow": {"oneOf": [EVENT_RECORD_SCHEMA, EVENT_CONTROL_FRAME_SCHEMA]},
    "events_follow_data": EVENT_RECORD_SCHEMA,
}


def approx_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def build_brief(
    verb_summaries: dict[str, dict[str, str]],
    exit_codes: dict[int, dict[str, Any]],
    job_states: set[str],
    env_docs: dict[str, str],
) -> tuple[str, int]:
    verbs = ", ".join(sorted(verb_summaries))
    states = ", ".join(sorted(job_states))
    exit_bits = ", ".join(
        f"{code}={info['meaning']}" for code, info in sorted(exit_codes.items())
    )
    lines = [
        "spoolctl quick brief",
        f"Verbs: {verbs}.",
        f"Jobs move through: {states}.",
        "JSON mode: every normal verb accepts --json and returns"
        " {ok, tool_version, data, meta, warnings, commands, errors}.",
        f"SPOOLCTL_DB: {env_docs['SPOOLCTL_DB']}. --db overrides it.",
        "Typical loop: spoolctl add -- <cmd>; spoolctl work --drain;"
        " spoolctl wait <ids>; spoolctl output <id> --stream stdout.",
        "Submit many jobs first, remember their ids, run one worker with"
        " work --drain, then wait on all ids. wait exits 6 when any awaited"
        " job ends non-success, but the JSON envelope is still ok:true and"
        " data.all_succeeded=false.",
        "add supports --after/--at, --priority, --queue, --key, --cwd,"
        " repeatable --env K=V, repeatable --tag KEY=VALUE, and --note;"
        " delayed jobs remain queued with future next_run_at.",
        "work serves one lane with --queue (default default); --slots N is an"
        " opt-in fleet-wide running ceiling for that lane.",
        "list filters by --state, repeatable --tag, --queue, and --priority-min;"
        " show prints full job, scheduling fields, attempts, events, key, tags,"
        " and note.",
        "status counts queued jobs inclusively and adds scheduled plus per-queue"
        " counts; scheduled means queued with future next_run_at, including"
        " retry backoff.",
        "events reads the durable job_events ledger: one-shot and --wait"
        " return envelopes with meta.pagination.cursor; --follow --json emits"
        " NDJSON data frames plus end/error control frames.",
        "schema --json exports the envelope, verb data, and raw stream JSON"
        " Schemas. capabilities --json describes contract v2 surfaces: flags,"
        " modes, states, events, process env, execution, safety gates,"
        " idempotency mismatches, exit codes, and robot_docs_uri.",
        "robot-docs guide is the longer paste-ready agent workflow handbook;"
        " use --json for structured sections and token estimate.",
        f"Exit codes: {exit_bits}.",
        "Use retry for dead/failed jobs, cancel for queued/running withdrawal,"
        " prune for old terminal jobs, status for counts/recent dead jobs."
        " Destructive forms require confirmations: prune uses --yes or"
        " --dry-run, cancel --running needs --yes, retry --force is the"
        " running-job recovery override.",
    ]
    prefix = "\n".join(lines)
    tokens = approx_tokens(prefix)
    while True:
        text = prefix + f"\n~{tokens} tokens (budget {BRIEF_BUDGET_TOKENS})."
        actual = approx_tokens(text)
        if actual == tokens:
            return text, actual
        tokens = actual
