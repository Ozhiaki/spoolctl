"""Reusable command operations for non-CLI adapters."""

from __future__ import annotations

import errno
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from spoolctl import store
from spoolctl.errors import CliError
from spoolctl.models import EXIT_CONFLICT

PREVIEW_BYTES = 4096


@dataclass(frozen=True)
class StatusInput:
    db_path: str | None
    limit: int
    now: Callable[[], float] = time.time


def _connect_db(db_path: str | None) -> sqlite3.Connection:
    resolved = store.resolve_db_path(db_path)
    try:
        return store.connect(resolved)
    except OSError as exc:
        if exc.errno in {
            errno.EACCES,
            errno.EEXIST,
            errno.ENOENT,
            errno.ENOTDIR,
            errno.EROFS,
        }:
            raise CliError(
                "INVALID_INPUT",
                f"cannot open queue database at {resolved!r}: {exc.strerror or exc}",
                "choose a writable database path with --db or SPOOLCTL_DB",
            ) from None
        raise
    except sqlite3.OperationalError as exc:
        text = str(exc)
        if "unable to open database file" in text or "readonly" in text:
            raise CliError(
                "INVALID_INPUT",
                f"cannot open queue database at {resolved!r}: {text}",
                "choose a writable database path with --db or SPOOLCTL_DB",
            ) from None
        raise


def status_operation(input: StatusInput) -> dict[str, Any]:
    conn = _connect_db(input.db_path)
    try:
        counts, scheduled, queues = store.status_counts(conn, input.now())
        dead = store.recent_dead(conn, input.limit)
    finally:
        conn.close()
    return {
        "counts": counts,
        "scheduled": scheduled,
        "queues": queues,
        "recent_dead": dead,
    }


@dataclass(frozen=True)
class OutputInput:
    db_path: str | None
    job_id: int
    attempt_no: int | None
    stream: str


@dataclass(frozen=True)
class OutputOperationResult:
    data: dict[str, Any] | None
    stream_bytes: dict[str, bytes]
    warnings: list[dict[str, str]]


@dataclass(frozen=True)
class AddInput:
    db_path: str | None
    argv: list[str]
    timeout: int
    max_retries: int
    max_crashes: int | None
    now: float
    key: str | None
    tags: dict[str, str]
    note: str | None
    priority: int
    queue: str
    next_run_at: float | None
    cwd: str | None
    env: dict[str, str]


@dataclass(frozen=True)
class AddOperationResult:
    data: dict[str, Any]
    deduplicated: bool
    state: str
    warnings: list[dict[str, str]]


def _read_stream(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""


def output_operation(input: OutputInput) -> OutputOperationResult:
    conn = _connect_db(input.db_path)
    try:
        job = store.get_job(conn, input.job_id)
        if job is None:
            raise CliError(
                "NOT_FOUND",
                f"no job with id {input.job_id}",
                "run: spoolctl status  (to list job ids)",
            )
        attempts = store.get_attempts(conn, input.job_id)
    finally:
        conn.close()
    if not attempts:
        return OutputOperationResult(
            data={"attempts": []},
            stream_bytes={},
            warnings=[{
                "code": "NO_ATTEMPTS_YET",
                "message": f"job {input.job_id} has not been executed yet",
            }],
        )
    if input.attempt_no is not None:
        matching = [a for a in attempts if a.attempt_no == input.attempt_no]
        if not matching:
            available = ", ".join(str(a.attempt_no) for a in attempts)
            raise CliError(
                "NOT_FOUND",
                f"job {input.job_id} has no attempt {input.attempt_no}",
                f"available attempts: {available}",
            )
        attempt = matching[0]
    else:
        attempt = attempts[-1]

    streams = ["stdout", "stderr"] if input.stream == "both" else [input.stream]
    paths = {"stdout": attempt.stdout_path, "stderr": attempt.stderr_path}
    stream_bytes = {name: _read_stream(paths[name]) for name in streams}
    stream_data = {
        name: {
            "path": paths[name],
            "preview": blob[:PREVIEW_BYTES].decode("utf-8", errors="replace"),
            "preview_truncated": len(blob) > PREVIEW_BYTES,
            "size_bytes": len(blob),
        }
        for name, blob in stream_bytes.items()
    }
    return OutputOperationResult(
        data={
            "attempt_no": attempt.attempt_no,
            "attempt_state": attempt.state,
            "attempts_total": len(attempts),
            "job_id": input.job_id,
            "streams": stream_data,
        },
        stream_bytes=stream_bytes,
        warnings=[],
    )


def add_operation(input: AddInput) -> AddOperationResult:
    conn = _connect_db(input.db_path)
    try:
        job_id, state, deduplicated, idempotency = store.add_job_checked(
            conn,
            input.argv,
            input.timeout,
            input.max_retries,
            input.now,
            input.key,
            input.tags,
            input.note,
            priority=input.priority,
            queue=input.queue,
            next_run_at=input.next_run_at,
            cwd=input.cwd,
            env=input.env,
            max_crashes=input.max_crashes,
        )
        if idempotency.get("execution_differences"):
            fields = ", ".join(idempotency["execution_differences"])
            raise CliError(
                "IDEMPOTENCY_CONFLICT",
                f"--key {input.key!r} is already active with a different execution payload"
                f" ({fields})",
                "reuse the key only with the same command, execution flags, queue,"
                " cwd, and environment; choose a new --key for different work",
                exit_code=EXIT_CONFLICT,
            )
        job = store.get_job(conn, job_id)
    finally:
        conn.close()

    metadata_differences = idempotency.get("metadata_differences", {})
    warnings = []
    if metadata_differences:
        fields = ", ".join(sorted(metadata_differences))
        warnings.append({
            "code": "IDEMPOTENCY_METADATA_DIFFERS",
            "message": f"active idempotent add ignored differing metadata: {fields}",
        })
    data = {
        "deduplicated": deduplicated,
        "cwd": job.cwd,
        "env_keys": sorted((job.env or {}).keys()),
        "job_id": job_id,
        "next_run_at": job.next_run_at,
        "priority": job.priority,
        "queue": job.queue,
        "state": state,
    }
    if deduplicated:
        data["idempotency"] = {
            "key": input.key,
            "metadata_differs": bool(metadata_differences),
            "metadata_differences": metadata_differences,
        }
    return AddOperationResult(
        data=data,
        deduplicated=deduplicated,
        state=state,
        warnings=warnings,
    )
