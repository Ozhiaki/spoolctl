"""Reusable status, output, add, and wait operations for non-CLI adapters."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from spoolctl import store
from spoolctl.config import default_config_path, resolve_effective_config, validate_config_file
from spoolctl.contract import build_capabilities, build_schema
from spoolctl.errors import CliError
from spoolctl.models import (
    CONTRACT_VERSION,
    EXIT_CONFLICT,
    EXIT_TRANSIENT,
    SCHEMA_VERSION,
    TOOL_VERSION,
    _WAIT_TERMINAL,
)

PREVIEW_BYTES = 4096


@dataclass(frozen=True)
class StatusInput:
    db_path: str | None
    limit: int
    base_dir: str | None = field(kw_only=True)
    now: Callable[[], float] = time.time


def _connect_db(db_path: str | None, *, base_dir: str | None) -> sqlite3.Connection:
    resolved = store.resolve_db_path(db_path, base_dir=base_dir)
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
    conn = _connect_db(input.db_path, base_dir=input.base_dir)
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
    base_dir: str | None = field(kw_only=True)


@dataclass(frozen=True)
class OutputOperationResult:
    data: dict[str, Any] | None
    stream_bytes: dict[str, bytes]
    warnings: list[dict[str, str]]


@dataclass(frozen=True)
class AddInput:
    # base_dir selects the project config discovery base. cwd is the submitted
    # job's working directory and intentionally remains separate.
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
    base_dir: str | None = field(kw_only=True)


@dataclass(frozen=True)
class AddOperationResult:
    data: dict[str, Any]
    deduplicated: bool
    state: str
    warnings: list[dict[str, str]]


@dataclass(frozen=True)
class WaitInput:
    db_path: str | None
    ids: list[int]
    timeout: float | None
    poll_interval: float
    base_dir: str | None = field(kw_only=True)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class WaitOperationResult:
    data: dict[str, Any]
    all_succeeded: bool


@dataclass(frozen=True)
class ConfigShowInput:
    db_path: str | None
    base_dir: str | None = field(kw_only=True)


@dataclass(frozen=True)
class ConfigValidateInput:
    path: str | None
    base_dir: str | None = field(kw_only=True)


@dataclass(frozen=True)
class DoctorInput:
    db_path: str | None
    base_dir: str | None = field(kw_only=True)
    parser_verbs: Mapping[str, Any] = field(kw_only=True)


def _read_stream(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""


def output_operation(input: OutputInput) -> OutputOperationResult:
    conn = _connect_db(input.db_path, base_dir=input.base_dir)
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
    conn = _connect_db(input.db_path, base_dir=input.base_dir)
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


def wait_operation(input: WaitInput) -> WaitOperationResult:
    id_list = " ".join(str(i) for i in input.ids)
    conn = _connect_db(input.db_path, base_dir=input.base_dir)
    try:
        missing = sorted({i for i in input.ids if store.get_job(conn, i) is None})
        if missing:
            raise CliError(
                "NOT_FOUND",
                "no job(s) with id(s): " + ", ".join(str(i) for i in missing),
                "run: spoolctl list  (to see job ids)",
            )
        deadline = None if input.timeout is None else input.monotonic() + input.timeout
        while True:
            jobs = {i: store.get_job(conn, i) for i in input.ids}
            if all(j.state in _WAIT_TERMINAL for j in jobs.values()):
                break
            if deadline is not None and input.monotonic() >= deadline:
                raise CliError(
                    "TIMEOUT",
                    f"jobs not settled after {input.timeout}s",
                    f"inspect crash counts with: spoolctl list --json  or: spoolctl show {input.ids[0]} --json; retry: spoolctl wait --timeout {input.timeout} {id_list}",
                    exit_code=EXIT_TRANSIENT,
                )
            input.sleep(input.poll_interval)
    finally:
        conn.close()
    all_succeeded = all(j.state == "done" for j in jobs.values())
    return WaitOperationResult(
        data={
            "all_succeeded": all_succeeded,
            "jobs": {
                str(i): {
                    "attempts": j.attempts,
                    "last_error": j.last_error,
                    "last_exit_code": j.last_exit_code,
                    "state": j.state,
                }
                for i, j in jobs.items()
            },
        },
        all_succeeded=all_succeeded,
    )


def config_show_operation(input: ConfigShowInput) -> dict[str, Any]:
    return resolve_effective_config(
        db_path_flag=input.db_path,
        base_dir=input.base_dir,
        strict_unknown_keys=False,
    ).as_dict()


def config_validate_operation(input: ConfigValidateInput) -> dict[str, Any]:
    if input.path is None:
        if input.base_dir is None:
            raise CliError(
                "INVALID_INPUT",
                "config validation needs a path or base_dir",
                "pass a config path or provide an explicit base_dir",
            )
        path = default_config_path(input.base_dir)
    else:
        path = input.path
    return validate_config_file(path).as_dict()


def _doctor_check(
    check_id: str,
    status: str,
    message: str,
    remediation: str | None = None,
    blocked_by: str | None = None,
) -> dict[str, str | None]:
    return {
        "id": check_id,
        "status": status,
        "message": message,
        "remediation": remediation,
        "blocked_by": blocked_by,
    }


def _doctor_skip(check_id: str, blocked_by: str) -> dict[str, str | None]:
    return _doctor_check(
        check_id,
        "skip",
        f"skipped because {blocked_by} did not pass",
        blocked_by=blocked_by,
    )


def _doctor_summary(checks: list[dict[str, str | None]]) -> dict[str, int]:
    return {
        "passed": sum(1 for c in checks if c["status"] == "pass"),
        "warnings": sum(1 for c in checks if c["status"] == "warn"),
        "failed": sum(1 for c in checks if c["status"] == "fail"),
        "skipped": sum(1 for c in checks if c["status"] == "skip"),
    }


def _sqlite_rw_uri(path: str) -> str:
    return "file:" + quote(path, safe="/:") + "?mode=rw"


def _read_schema_version_non_mutating(db_path: str) -> tuple[bool, int | None, str | None]:
    conn = sqlite3.connect(_sqlite_rw_uri(db_path), uri=True, timeout=1.0)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        return False, None, str(exc)
    conn.close()
    if row is None:
        return True, None, None
    try:
        return True, int(row[0]), None
    except (TypeError, ValueError):
        return True, None, f"schema_version is not an integer: {row[0]!r}"


def _contract_metadata_check(parser_verbs: Mapping[str, Any]) -> dict[str, str | None]:
    if not isinstance(parser_verbs, Mapping) or not parser_verbs:
        return _doctor_check(
            "contract_metadata",
            "fail",
            "adapter parser metadata is missing or invalid",
            "pass the live parser verb metadata into DoctorInput",
        )
    try:
        schema_data, _ = build_schema(None)
        caps_data, _ = build_capabilities(parser_verbs)
        json.dumps({
            "tool_version": TOOL_VERSION,
            "contract_version": CONTRACT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "schema": schema_data,
            "capabilities": caps_data,
        }, sort_keys=True)
    except Exception as exc:  # noqa: BLE001 - readiness diagnostic captures builder failures
        return _doctor_check(
            "contract_metadata",
            "fail",
            f"contract metadata could not be serialized: {exc}",
            "fix schema/capability builders before using this checkout",
        )
    return _doctor_check(
        "contract_metadata",
        "pass",
        "contract metadata is serializable",
    )


def doctor_operation(input: DoctorInput) -> dict[str, Any]:
    checks: list[dict[str, str | None]] = []
    config_data: dict[str, Any]
    try:
        effective = resolve_effective_config(
            db_path_flag=input.db_path,
            base_dir=input.base_dir,
            strict_unknown_keys=False,
        )
    except CliError as exc:
        if input.db_path is not None and exc.message.startswith("--db "):
            raise
        config_path = default_config_path(input.base_dir) if input.base_dir else None
        config_data = {
            "config_path": config_path,
            "config_exists": bool(config_path and os.path.exists(config_path)),
            "config_valid": False,
            "db_path": None,
            "db_source": None,
        }
        checks.append(_doctor_check(
            "config_valid",
            "fail",
            exc.message,
            exc.remediation,
        ))
        for check_id in (
            "db_path_resolved",
            "spool_directory_writable",
            "database_exists",
            "sqlite_open_readwrite",
            "schema_version",
        ):
            checks.append(_doctor_skip(check_id, "config_valid"))
        checks.append(_contract_metadata_check(input.parser_verbs))
        summary = _doctor_summary(checks)
        return {
            "ready": summary["failed"] == 0,
            "summary": summary,
            "config": config_data,
            "checks": checks,
            "versions": {
                "tool_version": TOOL_VERSION,
                "contract_version": CONTRACT_VERSION,
                "schema_version": SCHEMA_VERSION,
            },
        }

    config_data = {
        "config_path": effective.config_path,
        "config_exists": effective.config_exists,
        "config_valid": effective.config_valid,
        "db_path": effective.db_path,
        "db_source": effective.db_source,
    }
    checks.append(_doctor_check("config_valid", "pass", "configuration is valid"))
    checks.append(_doctor_check("db_path_resolved", "pass", "database path resolved"))

    db_path = effective.db_path
    parent = os.path.dirname(db_path) or "."
    if not os.path.isdir(parent):
        checks.append(_doctor_check(
            "spool_directory_writable",
            "fail",
            f"database directory does not exist: {parent}",
            "create the directory with a normal spoolctl command or choose an existing --db path",
        ))
    elif not os.access(parent, os.W_OK):
        checks.append(_doctor_check(
            "spool_directory_writable",
            "fail",
            f"database directory is not writable: {parent}",
            "choose a writable database directory",
        ))
    else:
        checks.append(_doctor_check(
            "spool_directory_writable",
            "pass",
            "database directory is writable",
        ))

    if not os.path.exists(db_path):
        checks.append(_doctor_check(
            "database_exists",
            "fail",
            f"database does not exist: {db_path}",
            "run a normal spoolctl command such as status to initialize the queue",
        ))
        checks.append(_doctor_skip("sqlite_open_readwrite", "database_exists"))
        checks.append(_doctor_skip("schema_version", "database_exists"))
    elif not os.path.isfile(db_path):
        checks.append(_doctor_check(
            "database_exists",
            "fail",
            f"database path is not a file: {db_path}",
            "choose a SQLite database file path",
        ))
        checks.append(_doctor_skip("sqlite_open_readwrite", "database_exists"))
        checks.append(_doctor_skip("schema_version", "database_exists"))
    else:
        checks.append(_doctor_check("database_exists", "pass", "database file exists"))
        try:
            opened, found, schema_error = _read_schema_version_non_mutating(db_path)
        except sqlite3.Error as exc:
            checks.append(_doctor_check(
                "sqlite_open_readwrite",
                "fail",
                f"database cannot be opened read/write: {exc}",
                "choose an existing writable SQLite database path",
            ))
            checks.append(_doctor_skip("schema_version", "sqlite_open_readwrite"))
        else:
            if not opened:
                checks.append(_doctor_check(
                    "sqlite_open_readwrite",
                    "fail",
                    f"database opened but schema metadata could not be read: {schema_error}",
                    "run a normal spoolctl command to initialize or migrate the queue",
                ))
                checks.append(_doctor_skip("schema_version", "sqlite_open_readwrite"))
            else:
                checks.append(_doctor_check(
                    "sqlite_open_readwrite",
                    "pass",
                    "database opened read/write without creating or migrating",
                ))
                if schema_error is not None:
                    checks.append(_doctor_check(
                        "schema_version",
                        "fail",
                        schema_error,
                        "run a normal spoolctl command to initialize or migrate the queue",
                    ))
                elif found is None:
                    checks.append(_doctor_check(
                        "schema_version",
                        "fail",
                        "database schema version metadata is missing",
                        "run a normal spoolctl command to initialize or migrate the queue",
                    ))
                elif found < SCHEMA_VERSION:
                    checks.append(_doctor_check(
                        "schema_version",
                        "fail",
                        f"database schema is {found}; expected {SCHEMA_VERSION}",
                        "run a normal spoolctl command to migrate the queue",
                    ))
                elif found > SCHEMA_VERSION:
                    checks.append(_doctor_check(
                        "schema_version",
                        "fail",
                        f"database schema is {found}; this spoolctl understands {SCHEMA_VERSION}",
                        "upgrade spoolctl before using this queue",
                    ))
                else:
                    checks.append(_doctor_check(
                        "schema_version",
                        "pass",
                        f"database schema is {SCHEMA_VERSION}",
                    ))

    checks.append(_contract_metadata_check(input.parser_verbs))
    summary = _doctor_summary(checks)
    return {
        "ready": summary["failed"] == 0,
        "summary": summary,
        "config": config_data,
        "checks": checks,
        "versions": {
            "tool_version": TOOL_VERSION,
            "contract_version": CONTRACT_VERSION,
            "schema_version": SCHEMA_VERSION,
        },
    }
