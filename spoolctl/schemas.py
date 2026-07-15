"""JSON Schema definitions for spoolctl's machine contract.

The schemas are runtime data exported by the future `schema` verb. Validation
stays test-only so the CLI keeps zero runtime dependencies.
"""

from __future__ import annotations

import math
from typing import Any

from spoolctl.models import ATTEMPT_STATES, EXIT_CODES, JOB_EVENT_TYPES, JOB_STATES

DIALECT = "https://json-schema.org/draft/2020-12/schema"
BRIEF_BUDGET_TOKENS = 700

NULLABLE_STRING = {"type": ["string", "null"]}
NULLABLE_INTEGER = {"type": ["integer", "null"]}
NULLABLE_NUMBER = {"type": ["number", "null"]}


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
    "code": {"type": "string"},
    "message": {"type": "string"},
    "remediation": {"type": "string"},
    "exit_code": {"type": "integer"},
    "did_you_mean": NULLABLE_STRING,
}, required=["code", "message", "remediation", "exit_code"])

WARNING_SCHEMA = obj({
    "code": {"type": "string"},
    "message": {"type": "string"},
}, required=["code", "message"])

PAGINATION_SCHEMA = obj({
    "cursor": {"type": "integer"},
    "first_id": NULLABLE_INTEGER,
})

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
    "created_at": {"type": "number"},
    "finished_at": NULLABLE_NUMBER,
    "id": {"type": "integer"},
    "last_error": NULLABLE_STRING,
    "last_exit_code": NULLABLE_INTEGER,
    "max_retries": {"type": "integer"},
    "next_run_at": {"type": "number"},
    "started_at": NULLABLE_NUMBER,
    "state": {"type": "string", "enum": list(JOB_STATES)},
    "timeout_seconds": {"type": "integer"},
    **JOB_METADATA_PROPS,
})

SHOW_JOB_SCHEMA = obj({
    **LIST_JOB_SCHEMA["properties"],
    "heartbeat_at": NULLABLE_NUMBER,
    "locked_at": NULLABLE_NUMBER,
    "locked_by": NULLABLE_STRING,
    "locked_pid": NULLABLE_INTEGER,
})

ATTEMPT_SCHEMA = obj({
    "attempt_no": {"type": "integer"},
    "error": NULLABLE_STRING,
    "exit_code": NULLABLE_INTEGER,
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

VERB_SCHEMAS = {
    "add": obj({
        "deduplicated": {"type": "boolean"},
        "job_id": {"type": "integer"},
        "state": {"type": "string", "enum": ["queued", "running"]},
    }),
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
        "recent_dead": array_of(obj({
            "attempts": {"type": "integer"},
            "command": {"type": "string"},
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
        "deleted_attempts": {"type": "integer"},
        "deleted_events": {"type": "integer"},
        "deleted_jobs": {"type": "integer"},
        "dry_run": {"type": "boolean"},
        "freed_bytes": {"type": "integer"},
        "matched": {"type": "integer"},
    }),
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
    "brief": obj({
        "approx_tokens": {"type": "integer"},
        "budget_tokens": {"type": "integer", "const": BRIEF_BUDGET_TOKENS},
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
        "contract_policy": {"type": "string"},
        "contract_version": {"type": "string"},
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
        "error_codes": array_of({"type": "string"}),
        "events": array_of({"type": "string"}),
        "exit_codes": {
            "type": "object",
            "additionalProperties": obj({
                "meaning": {"type": "string"},
                "note": {"type": "string"},
                "retryable": {"type": ["boolean", "null"]},
            }, required=["meaning", "retryable"]),
        },
        "job_states": array_of({"type": "string"}),
        "verbs": {"type": "object", "additionalProperties": {}},
    }),
}

STREAM_SCHEMAS = {
    "events_follow": EVENT_RECORD_SCHEMA,
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
        "add supports --key for active queued/running dedup and repeatable"
        " --tag KEY=VALUE plus --note for immutable handoff metadata.",
        "list filters by --state and repeatable --tag; show prints full job,"
        " attempts, events, key, tags, and note.",
        "events reads the durable job_events ledger: one-shot and --wait"
        " return envelopes with meta.pagination.cursor; --follow --json emits"
        " raw NDJSON event records only, no control frames.",
        "schema --json exports the envelope, verb data, and raw stream JSON"
        " Schemas. capabilities --json describes flags, modes, states, events,"
        " env, and exit codes.",
        f"Exit codes: {exit_bits}.",
        "Use retry for dead/failed jobs, cancel for queued/running withdrawal,"
        " prune for old terminal jobs, status for counts/recent dead jobs.",
    ]
    prefix = "\n".join(lines)
    tokens = approx_tokens(prefix)
    while True:
        text = prefix + f"\n~{tokens} tokens (budget {BRIEF_BUDGET_TOKENS})."
        actual = approx_tokens(text)
        if actual == tokens:
            return text, actual
        tokens = actual
