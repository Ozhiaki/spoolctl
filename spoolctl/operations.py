"""Reusable status, output, add, and wait operations for non-CLI adapters."""

from __future__ import annotations

import errno
import json
import os
import shlex
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
    EXIT_SAFETY,
    EXIT_TRANSIENT,
    SCHEMA_VERSION,
    TAG_FILTER_SCAN_LIMIT,
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
class EventsInput:
    """One-shot event page, optionally preceded by a bounded long-poll.

    --follow is deliberately not modeled here; it is a live stream and stays
    in the adapter. See the module docstring.
    """

    db_path: str | None
    since_id: int
    job_id: int | None
    limit: int
    wait: bool
    wait_timeout: float
    poll_interval: float
    base_dir: str | None = field(kw_only=True)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class EventsOperationResult:
    data: dict[str, Any]
    events: list[dict[str, Any]]
    pagination: dict[str, int | None]
    wait: dict[str, Any] | None


@dataclass(frozen=True)
class ListInput:
    db_path: str | None
    states: list[str] | None
    tag_predicates: list[tuple[str, str | None]]
    queue: str | None
    priority_min: int | None
    effective_limit: int
    base_dir: str | None = field(kw_only=True)


@dataclass(frozen=True)
class ListOperationResult:
    data: dict[str, Any]
    jobs: list[store.Job]
    pagination: dict[str, Any]
    warnings: list[dict[str, str]]


@dataclass(frozen=True)
class PruneInput:
    """dry_run is the already-decided effect, not the adapter's --dry-run flag.

    The --yes / --dry-run consent gate lives in the adapter; the operation
    only sees whether to select or to delete.
    """

    db_path: str | None
    states: list[str]
    cutoff: float
    dry_run: bool
    base_dir: str | None = field(kw_only=True)


@dataclass(frozen=True)
class CancelInput:
    """running is the already-decided effect, not the adapter's --running flag.

    The --running --yes consent gate lives in the adapter; the operation only
    sees the decision.
    """

    db_path: str | None
    job_id: int
    running: bool
    now: float
    base_dir: str | None = field(kw_only=True)


@dataclass(frozen=True)
class CancelOperationResult:
    data: dict[str, Any]
    warnings: list[dict[str, str]]


@dataclass(frozen=True)
class RetryInput:
    """force is the already-decided effect, not the adapter's --force flag.

    Consent lives in the adapter; the operation only sees the decision.
    """

    db_path: str | None
    job_id: int
    force: bool
    now: float
    base_dir: str | None = field(kw_only=True)


@dataclass(frozen=True)
class ShowInput:
    db_path: str | None
    job_id: int
    base_dir: str | None = field(kw_only=True)


@dataclass(frozen=True)
class ShowOperationResult:
    data: dict[str, Any]
    job: store.Job
    attempts: list[store.Attempt]
    events: list[dict[str, Any]]


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


def _event_page(
    conn: sqlite3.Connection,
    since_id: int,
    job_id: int | None,
    limit: int,
) -> tuple[list[dict], dict[str, int | None]]:
    fetch_limit = 0 if limit == 0 else limit + 1
    rows = store.list_events(conn, since_id, job_id, fetch_limit)
    truncated = limit > 0 and len(rows) > limit
    events = rows[:limit] if truncated else rows
    high_water = store.event_high_water(conn)
    if truncated:
        cursor = events[-1]["id"]
    else:
        cursor = max(since_id, high_water)
    return events, {
        "cursor": cursor,
        "first_id": store.first_event_id(conn, job_id),
    }


def events_operation(input: EventsInput) -> EventsOperationResult:
    conn = _connect_db(input.db_path, base_dir=input.base_dir)
    try:
        waited_start = input.monotonic()
        wait_reason = None
        if input.wait:
            deadline = waited_start + input.wait_timeout
            while True:
                probe = store.list_events(conn, input.since_id, input.job_id, 1)
                if probe:
                    wait_reason = "records_available"
                    break
                if input.monotonic() >= deadline:
                    wait_reason = "timeout"
                    break
                input.sleep(input.poll_interval)
        events, pagination = _event_page(
            conn, input.since_id, input.job_id, input.limit
        )
    finally:
        conn.close()
    wait_meta = None
    if input.wait:
        wait_meta = {
            "reason": wait_reason,
            "waited_ms": int((input.monotonic() - waited_start) * 1000),
        }
    return EventsOperationResult(
        data={"count": len(events), "events": events},
        events=events,
        pagination=pagination,
        wait=wait_meta,
    )


def _tags_match(tags: dict[str, str], predicates: list[tuple[str, str | None]]) -> bool:
    for key, value in predicates:
        if key not in tags:
            return False
        if value is not None and tags[key] != value:
            return False
    return True


def list_operation(input: ListInput) -> ListOperationResult:
    tag_predicates = input.tag_predicates
    effective_limit = input.effective_limit
    fetch_limit = (
        TAG_FILTER_SCAN_LIMIT + 1 if tag_predicates else effective_limit + 1
    )
    conn = _connect_db(input.db_path, base_dir=input.base_dir)
    try:
        jobs = store.list_jobs(
            conn, input.states, fetch_limit,
            queue=input.queue, priority_min=input.priority_min,
        )
        first_id, _ = store.job_id_bounds(conn)
    finally:
        conn.close()
    scanned = len(jobs)
    scan_truncated = bool(tag_predicates and len(jobs) > TAG_FILTER_SCAN_LIMIT)
    if tag_predicates:
        scan_rows = jobs[:TAG_FILTER_SCAN_LIMIT]
        filtered = [j for j in scan_rows if _tags_match(j.tags or {}, tag_predicates)]
        truncated = scan_truncated or len(filtered) > effective_limit
        jobs = filtered[:effective_limit]
    else:
        truncated = len(jobs) > effective_limit
        jobs = jobs[:effective_limit]
    rows = [
        {
            "argv": j.argv,
            "attempts": j.attempts,
            "created_at": j.created_at,
            "finished_at": j.finished_at,
            "id": j.id,
            "idempotency_key": j.idempotency_key,
            "last_error": j.last_error,
            "last_exit_code": j.last_exit_code,
            "max_retries": j.max_retries,
            "crashes": j.crashes,
            "cwd": j.cwd,
            "next_run_at": j.next_run_at,
            "note": j.note,
            "priority": j.priority,
            "queue": j.queue,
            "started_at": j.started_at,
            "state": j.state,
            "tags": j.tags or {},
            "timeout_seconds": j.timeout_seconds,
        }
        for j in jobs
    ]
    pagination = {
        "cursor": jobs[-1].id if jobs else 0,
        "first_id": first_id,
        "limit": effective_limit,
        "truncated": truncated,
    }
    if tag_predicates:
        pagination["scanned"] = min(scanned, TAG_FILTER_SCAN_LIMIT)
        pagination["scan_limit"] = TAG_FILTER_SCAN_LIMIT
    warnings = []
    if scan_truncated:
        warnings.append({
            "code": "TAG_FILTER_SCAN_CAPPED",
            "message": (
                "tag-filtered list scanned the newest"
                f" {TAG_FILTER_SCAN_LIMIT} matching pre-filter rows; later"
                " tag matches may be omitted"
            ),
        })
    return ListOperationResult(
        data={"count": len(rows), "jobs": rows},
        jobs=jobs,
        pagination=pagination,
        warnings=warnings,
    )


def prune_operation(input: PruneInput) -> dict[str, Any]:
    conn = _connect_db(input.db_path, base_dir=input.base_dir)
    try:
        matches = store.prune_matches(conn, input.states, input.cutoff)
        if input.dry_run:
            freed = 0
            for m in matches:
                for paths in m["paths"]:
                    for path in paths:
                        try:
                            freed += os.stat(path).st_size
                        except OSError:
                            pass
            return {
                "deleted_attempts": sum(m["n_attempts"] for m in matches),
                "deleted_events": sum(m["n_events"] for m in matches),
                "deleted_jobs": len(matches),
                "actual": False,
                "dry_run": True,
                "freed_bytes": freed,
                "matched": len(matches),
            }
        # Files first, rows second: a crash in between leaves rows a re-run
        # still finds; the reverse order would strand invisible orphan files.
        freed = 0
        for m in matches:
            for stdout_path, stderr_path in m["paths"]:
                for path in (stdout_path, stderr_path):
                    try:
                        freed += os.stat(path).st_size
                        os.unlink(path)
                    except OSError:
                        pass  # already gone; re-runs must not trip here
                try:
                    os.rmdir(os.path.dirname(stdout_path))
                except OSError:
                    pass
            if m["paths"]:
                try:
                    os.rmdir(os.path.dirname(os.path.dirname(m["paths"][0][0])))
                except OSError:
                    pass  # not empty (e.g. a newer attempt's dir) or gone
        jobs, attempts, events = store.prune_delete(
            conn, [m["job_id"] for m in matches], input.states, input.cutoff)
    finally:
        conn.close()
    return {
        "deleted_attempts": attempts,
        "deleted_events": events,
        "deleted_jobs": jobs,
        "actual": True,
        "dry_run": False,
        "freed_bytes": freed,
        "irreversible": True,
        "matched": len(matches),
    }


def cancel_operation(input: CancelInput) -> CancelOperationResult:
    """Record the cancellation. Nothing is killed here.

    On the running path the owning worker observes lost ownership and kills
    its own child process group; that logic lives in worker.py.
    """
    job_id = input.job_id
    conn = _connect_db(input.db_path, base_dir=input.base_dir)
    try:
        outcome, state = store.cancel_job(conn, job_id, input.running, input.now)
    finally:
        conn.close()
    if outcome == "ok":
        return CancelOperationResult(
            data={"job_id": job_id, "state": "canceled", "was_running": False},
            warnings=[],
        )
    if outcome == "ok_running":
        return CancelOperationResult(
            data={"job_id": job_id, "state": "canceled", "was_running": True},
            warnings=[{
                "code": "KILL_ASYNC",
                "message": "the job's process dies within about one heartbeat"
                           " interval, not synchronously",
            }],
        )
    if outcome == "not_found":
        raise CliError(
            "NOT_FOUND",
            f"no job with id {job_id}",
            "run: spoolctl list  (to see job ids)",
        )
    if outcome == "running_unforced":
        raise CliError(
            "SAFETY_BLOCK",
            f"job {job_id} is running; canceling it kills its process",
            "let it finish, or force with:"
            f" spoolctl cancel --running --yes {job_id}",
            exit_code=EXIT_SAFETY,
        )
    if outcome == "raced":
        raise CliError(
            "CONFLICT",
            f"job {job_id} changed state before --running could cancel it"
            f" (now {state})",
            f"re-check with: spoolctl show {job_id}",
            exit_code=EXIT_CONFLICT,
        )
    # terminal: done / dead / canceled (or failed)
    if state == "dead":
        remediation = f"to run it again: spoolctl retry {job_id}"
    elif state == "canceled":
        remediation = "nothing to do; it is already canceled"
    else:
        remediation = f"nothing to cancel; the job already finished ({state})"
    raise CliError(
        "CONFLICT",
        f"job {job_id} is already {state}",
        remediation,
        exit_code=EXIT_CONFLICT,
    )


def retry_operation(input: RetryInput) -> dict[str, Any]:
    job_id = input.job_id
    conn = _connect_db(input.db_path, base_dir=input.base_dir)
    try:
        outcome, argv = store.retry_job(conn, job_id, input.force, input.now)
    finally:
        conn.close()
    if outcome == "ok":
        return {"job_id": job_id, "state": "queued"}
    if outcome == "not_found":
        raise CliError(
            "NOT_FOUND",
            f"no job with id {job_id}",
            "run: spoolctl status  (to list job ids)",
        )
    if outcome == "already_queued":
        raise CliError(
            "CONFLICT",
            f"job {job_id} is already queued",
            "run: spoolctl work  (to execute it)",
            exit_code=EXIT_CONFLICT,
        )
    if outcome == "done":
        readd = " ".join(shlex.quote(t) for t in argv)
        raise CliError(
            "CONFLICT",
            f"job {job_id} already succeeded; retry would rerun a completed job",
            f"try: spoolctl add -- {readd}",
            exit_code=EXIT_CONFLICT,
        )
    if outcome == "running_unforced":
        raise CliError(
            "SAFETY_BLOCK",
            f"job {job_id} is running; requeuing it could execute the job twice",
            "wait for automatic recovery (the reaper requeues it once the owning"
            f" worker is confirmed dead), or force with: spoolctl retry --force {job_id}",
            exit_code=EXIT_SAFETY,
        )
    # raced: force re-check found the row no longer running
    raise CliError(
        "CONFLICT",
        f"job {job_id} changed state before --force could requeue it",
        f"re-check with: spoolctl status, then: spoolctl retry {job_id}",
        exit_code=EXIT_CONFLICT,
    )


def current_job_failure_reason(job: store.Job, attempts: list[store.Attempt]) -> str | None:
    if job.state in {"done", "queued", "running"}:
        return None
    for attempt in sorted(attempts, key=lambda a: a.attempt_no, reverse=True):
        if attempt.state != "succeeded" and attempt.failure_reason is not None:
            return attempt.failure_reason
    return None


def show_operation(input: ShowInput) -> ShowOperationResult:
    conn = _connect_db(input.db_path, base_dir=input.base_dir)
    try:
        job = store.get_job(conn, input.job_id)
        if job is None:
            raise CliError(
                "NOT_FOUND",
                f"no job with id {input.job_id}",
                "run: spoolctl list  (to see job ids)",
            )
        attempts = store.get_attempts(conn, input.job_id)
        events = store.get_events(conn, input.job_id)
    finally:
        conn.close()
    job_data = {
        "argv": job.argv,
        "attempts": job.attempts,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "heartbeat_at": job.heartbeat_at,
        "id": job.id,
        "idempotency_key": job.idempotency_key,
        "last_error": job.last_error,
        "last_exit_code": job.last_exit_code,
        "last_failure_reason": current_job_failure_reason(job, attempts),
        "locked_at": job.locked_at,
        "locked_by": job.locked_by,
        "locked_pid": job.locked_pid,
        "max_retries": job.max_retries,
        "crashes": job.crashes,
        "cwd": job.cwd,
        "env": job.env or {},
        "max_crashes": job.max_crashes,
        "next_run_at": job.next_run_at,
        "note": job.note,
        "priority": job.priority,
        "queue": job.queue,
        "started_at": job.started_at,
        "state": job.state,
        "tags": job.tags or {},
        "timeout_seconds": job.timeout_seconds,
    }
    attempt_rows = [
        {
            "attempt_no": a.attempt_no,
            "error": a.error,
            "exit_code": a.exit_code,
            "failure_reason": a.failure_reason,
            "finished_at": a.finished_at,
            "started_at": a.started_at,
            "state": a.state,
            "stderr_path": a.stderr_path,
            "stdout_path": a.stdout_path,
            "worker_id": a.worker_id,
            "worker_pid": a.worker_pid,
        }
        for a in attempts
    ]
    return ShowOperationResult(
        data={"attempts": attempt_rows, "events": events, "job": job_data},
        job=job,
        attempts=attempts,
        events=events,
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
