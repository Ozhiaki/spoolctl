"""Data model: constants, tunables, and row dataclasses.

Single source of truth for state names, retry/backoff tunables, the CLI
contract version, and the exit-code / error-code dictionaries. No I/O here.
"""

from __future__ import annotations

from dataclasses import dataclass

TOOL_VERSION = "0.4.2"
CONTRACT_VERSION = "1"
SCHEMA_VERSION = 6

# Job states
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
DEAD = "dead"
CANCELED = "canceled"
JOB_STATES = (QUEUED, RUNNING, DONE, FAILED, DEAD, CANCELED)

# Attempt states
ATT_RUNNING = "running"
ATT_SUCCEEDED = "succeeded"
ATT_FAILED = "failed"
ATT_TIMED_OUT = "timed_out"
ATT_ABANDONED = "abandoned"
ATT_CANCELED = "canceled"
ATTEMPT_STATES = (
    ATT_RUNNING, ATT_SUCCEEDED, ATT_FAILED, ATT_TIMED_OUT, ATT_ABANDONED, ATT_CANCELED
)

# Attempt failure reasons
REASON_PROCESS_EXIT = "process_exit"
REASON_TIMEOUT = "timeout"
REASON_SPAWN_FAILED = "spawn_failed"
REASON_WORKER_CRASH = "worker_crash"
REASON_CANCELED = "canceled"
REASON_UNKNOWN = "unknown"
FAILURE_REASONS = (
    REASON_PROCESS_EXIT,
    REASON_TIMEOUT,
    REASON_SPAWN_FAILED,
    REASON_WORKER_CRASH,
    REASON_CANCELED,
    REASON_UNKNOWN,
)

# job_events.event values (additive under contract_version 1)
JOB_EVENT_TYPES = (
    "added", "claimed", "succeeded", "failed", "timed_out",
    "reaped", "dead", "retried", "canceled",
)

# Retry / reaping tunables (constants by ruling A2, not flags)
BACKOFF_BASE = 2
BACKOFF_CAP = 60
HEARTBEAT_INTERVAL = 5.0
REAP_THRESHOLD = max(4 * HEARTBEAT_INTERVAL, 30.0)
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_RETRIES = 3
DEFAULT_POLL_INTERVAL = 1.0
KILL_GRACE_SECONDS = 5.0
BUSY_TIMEOUT_MS = 5000

# Exit-code dictionary (5.2) — the only codes any path may use
EXIT_OK = 0
EXIT_INPUT = 1
EXIT_SAFETY = 2
EXIT_ENVIRONMENT = 3
EXIT_TRANSIENT = 4
EXIT_CONFLICT = 5
EXIT_JOB_FAILURE = 6

EXIT_CODES = {
    EXIT_OK: {"meaning": "success (including empty results)", "retryable": None},
    EXIT_INPUT: {"meaning": "user-input-error", "retryable": False},
    EXIT_SAFETY: {"meaning": "safety-block (refused; a --force form may exist)", "retryable": False},
    EXIT_ENVIRONMENT: {"meaning": "tool-environment-error", "retryable": None},
    EXIT_TRANSIENT: {"meaning": "transient-failure (retry after a short delay)", "retryable": True},
    EXIT_CONFLICT: {"meaning": "conflict (state changed underneath)", "retryable": False},
    EXIT_JOB_FAILURE: {
        "meaning": "job-outcome-failure (an awaited job ended non-success)",
        # The one deliberate exception to "non-zero exit implies errors
        # populated": exit 6 pairs with ok:true and empty errors[] — the tool
        # call succeeded; the exit code carries job outcome for shell use.
        # Envelope consumers key off data.all_succeeded, not the exit code.
        "note": "pairs with ok:true and empty errors[]; the tool call"
                " succeeded and the exit code carries the job outcome for"
                " shell use; envelope consumers key off data.all_succeeded,"
                " not the exit code",
        "retryable": False,
    },
}

# Closed code registry. `appears_in` distinguishes machine error codes from
# warning codes while keeping one discoverable contract surface.
CODE_REGISTRY = {
    "INVALID_INPUT": {
        "appears_in": ["errors"],
        "summary": "A supplied value has the wrong type, range, enum, or grammar.",
        "exit_code": EXIT_INPUT,
        "retryable": False,
        "example": "spoolctl output --json 1 --stream stdou",
    },
    "MISSING_REQUIRED": {
        "appears_in": ["errors"],
        "summary": "A required verb, flag value, or positional argument is absent.",
        "exit_code": EXIT_INPUT,
        "retryable": False,
        "example": "spoolctl --json",
    },
    "UNKNOWN_FLAG": {
        "appears_in": ["errors"],
        "summary": "A flag is not accepted by the selected command surface.",
        "exit_code": EXIT_INPUT,
        "retryable": False,
        "example": "spoolctl status --jsno",
    },
    "UNKNOWN_COMMAND": {
        "appears_in": ["errors"],
        "summary": "The requested verb is not implemented by this CLI.",
        "exit_code": EXIT_INPUT,
        "retryable": False,
        "example": "spoolctl statu --json",
    },
    "NOT_FOUND": {
        "appears_in": ["errors"],
        "summary": "The requested job or attempt does not exist.",
        "exit_code": EXIT_INPUT,
        "retryable": False,
        "example": "spoolctl show 999 --json",
    },
    "CONFLICT": {
        "appears_in": ["errors"],
        "summary": "The requested mutation conflicts with current job state.",
        "exit_code": EXIT_CONFLICT,
        "retryable": False,
        "example": "spoolctl retry 1 --json when job 1 is queued",
    },
    "IDEMPOTENCY_CONFLICT": {
        "appears_in": ["errors"],
        "summary": "An active idempotency key was reused with a different execution payload.",
        "exit_code": EXIT_CONFLICT,
        "retryable": False,
        "example": "spoolctl add --key daily -- false after active daily=true",
    },
    "SAFETY_BLOCK": {
        "appears_in": ["errors"],
        "summary": "A destructive or interrupting action was refused without explicit confirmation.",
        "exit_code": EXIT_SAFETY,
        "retryable": False,
        "example": "spoolctl retry 1 --json while job 1 is running",
    },
    "LOCKED": {
        "appears_in": ["errors"],
        "summary": "The queue database stayed busy beyond the configured lock timeout.",
        "exit_code": EXIT_TRANSIENT,
        "retryable": True,
        "example": "spoolctl add --json -- true during a long write lock",
    },
    "TIMEOUT": {
        "appears_in": ["errors"],
        "summary": "A wait-style operation exceeded its declared time budget.",
        "exit_code": EXIT_TRANSIENT,
        "retryable": True,
        "example": "spoolctl wait 1 --timeout 0.1 --json",
    },
    "INTERNAL": {
        "appears_in": ["errors"],
        "summary": "The CLI or environment failed outside a user-input contract path.",
        "exit_code": EXIT_ENVIRONMENT,
        "retryable": None,
        "example": "Opening a database with a schema version newer than this binary.",
    },
    "KILL_ASYNC": {
        "appears_in": ["warnings"],
        "summary": "A running cancel has been recorded; process-group termination is asynchronous.",
        "retryable": None,
        "example": "spoolctl cancel --running --yes 1 --json",
    },
    "NO_ATTEMPTS_YET": {
        "appears_in": ["warnings"],
        "summary": "A job exists but no attempt output can be read yet.",
        "retryable": None,
        "example": "spoolctl output 1 --json before a worker runs job 1",
    },
    "IDEMPOTENCY_METADATA_DIFFERS": {
        "appears_in": ["warnings"],
        "summary": "An active idempotent add matched execution payload but ignored metadata differences.",
        "retryable": None,
        "example": "spoolctl add --key daily --note changed -- true",
    },
    "TAG_FILTER_SCAN_CAPPED": {
        "appears_in": ["warnings"],
        "summary": "A list tag filter scanned a bounded prefix and may have omitted later matches.",
        "retryable": None,
        "example": "spoolctl list --tag owner=agent --limit 0 --json",
    },
}

ERROR_CODES = tuple(
    code for code, entry in CODE_REGISTRY.items() if "errors" in entry["appears_in"]
)
WARNING_CODES = tuple(
    code for code, entry in CODE_REGISTRY.items() if "warnings" in entry["appears_in"]
)


def backoff_seconds(attempts: int) -> float:
    """Delay before the next run after `attempts` failed executions (>= 1)."""
    return float(min(BACKOFF_CAP, BACKOFF_BASE * 2 ** (attempts - 1)))


@dataclass
class Job:
    id: int
    argv: list[str]
    state: str
    attempts: int
    max_retries: int
    timeout_seconds: int
    created_at: float
    next_run_at: float
    priority: int = 0
    queue: str = "default"
    locked_by: str | None = None
    locked_pid: int | None = None
    locked_at: float | None = None
    heartbeat_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    last_exit_code: int | None = None
    last_error: str | None = None
    idempotency_key: str | None = None
    tags: dict[str, str] | None = None
    note: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    crashes: int = 0
    max_crashes: int | None = None


@dataclass
class Attempt:
    id: int
    job_id: int
    attempt_no: int
    worker_id: str
    worker_pid: int
    state: str
    started_at: float
    stdout_path: str
    stderr_path: str
    finished_at: float | None = None
    exit_code: int | None = None
    error: str | None = None
    failure_reason: str | None = None
