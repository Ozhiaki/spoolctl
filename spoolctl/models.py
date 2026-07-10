"""Data model: constants, tunables, and row dataclasses.

Single source of truth for state names, retry/backoff tunables, the CLI
contract version, and the exit-code / error-code dictionaries. No I/O here.
"""

from __future__ import annotations

from dataclasses import dataclass

TOOL_VERSION = "0.1.0"
CONTRACT_VERSION = "1"
SCHEMA_VERSION = 2

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

EXIT_CODES = {
    EXIT_OK: {"meaning": "success (including empty results)", "retryable": None},
    EXIT_INPUT: {"meaning": "user-input-error", "retryable": False},
    EXIT_SAFETY: {"meaning": "safety-block (refused; a --force form may exist)", "retryable": False},
    EXIT_ENVIRONMENT: {"meaning": "tool-environment-error", "retryable": None},
    EXIT_TRANSIENT: {"meaning": "transient-failure (retry after a short delay)", "retryable": True},
    EXIT_CONFLICT: {"meaning": "conflict (state changed underneath)", "retryable": False},
}

# Error codes (5.3)
ERROR_CODES = (
    "INVALID_INPUT",
    "MISSING_REQUIRED",
    "UNKNOWN_FLAG",
    "UNKNOWN_COMMAND",
    "NOT_FOUND",
    "CONFLICT",
    "SAFETY_BLOCK",
    "LOCKED",
    "TIMEOUT",
    "INTERNAL",
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
    locked_by: str | None = None
    locked_pid: int | None = None
    locked_at: float | None = None
    heartbeat_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    last_exit_code: int | None = None
    last_error: str | None = None


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
