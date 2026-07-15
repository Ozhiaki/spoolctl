"""JSON Schema definitions for spoolctl's machine contract.

The schemas are runtime data exported by the future `schema` verb. Validation
stays test-only so the CLI keeps zero runtime dependencies.
"""

from __future__ import annotations

from spoolctl.models import ATTEMPT_STATES, EXIT_CODES, JOB_EVENT_TYPES, JOB_STATES

DIALECT = "https://json-schema.org/draft/2020-12/schema"

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
