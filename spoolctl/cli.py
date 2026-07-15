"""argparse dispatch, envelope construction, human rendering, exit codes.

Thin over store/worker. Every verb speaks one machine contract: the
seven-key JSON envelope, the published exit-code dictionary, and errors
that teach the corrected invocation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from spoolctl import store
from spoolctl.models import (
    ATTEMPT_STATES,
    CONTRACT_VERSION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIMEOUT_SECONDS,
    ERROR_CODES,
    EXIT_CODES,
    EXIT_CONFLICT,
    EXIT_ENVIRONMENT,
    EXIT_INPUT,
    EXIT_JOB_FAILURE,
    EXIT_OK,
    EXIT_SAFETY,
    EXIT_TRANSIENT,
    JOB_EVENT_TYPES,
    JOB_STATES,
    TOOL_VERSION,
)

HELP_EPILOG = """\
AGENT/AUTOMATION:
  Run `spoolctl capabilities --json` for the full machine-readable contract:
  verbs, flags, data schemas, exit codes, error codes.
"""


class CliError(Exception):
    """A contract error: code + message + remediation + exit code."""

    def __init__(
        self,
        code: str,
        message: str,
        remediation: str,
        exit_code: int = EXIT_INPUT,
        did_you_mean: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation
        self.exit_code = exit_code
        self.did_you_mean = did_you_mean

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
            "exit_code": self.exit_code,
        }
        if self.did_you_mean is not None:
            d["did_you_mean"] = self.did_you_mean
        return d


@dataclass
class VerbResult:
    """What a verb handler returns; the framework wraps it.

    exit_code other than EXIT_OK is only for the documented ok:true
    exception (wait's exit 6): the envelope still reports success with
    empty errors; the exit code carries job outcome for shell use."""

    data: Any
    human: str
    warnings: list[dict[str, str]] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    meta_extra: dict[str, Any] | None = None
    stdout_silent: bool = False  # loop-mode work: nothing on stdout
    exit_code: int = EXIT_OK


class _ParserExit(Exception):
    def __init__(self, message: str, parser: argparse.ArgumentParser):
        super().__init__(message)
        self.parser = parser


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of sys.exit'ing, so errors flow through
    the envelope with the published codes rather than argparse's exit 2."""

    def error(self, message: str):  # noqa: A002 - argparse API
        raise _ParserExit(message, self)


def _levenshtein_leq1(a: str, b: str) -> bool:
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:  # one substitution
        return sum(x != y for x, y in zip(a, b)) <= 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    # one insertion into a
    i = j = edits = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            edits += 1
            if edits > 1:
                return False
            j += 1
    return True


def _suggest(word: str, candidates: list[str]) -> str | None:
    for c in sorted(candidates):
        if c != word and _levenshtein_leq1(word, c):
            return c
    return None


# --- envelope -----------------------------------------------------------


def canonical_data_hash(data: Any) -> str:
    canon = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def make_envelope(
    data: Any,
    *,
    started: float,
    warnings: list[dict[str, str]] | None = None,
    commands: list[str] | None = None,
    meta_extra: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    meta = {
        "request_id": "req_" + uuid.uuid4().hex[:12],
        "ts_iso": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "contract_version": CONTRACT_VERSION,
        "data_hash": canonical_data_hash(data),
    }
    if meta_extra:
        overlap = set(meta) & set(meta_extra)
        if overlap:
            raise ValueError(
                "meta_extra cannot override base meta keys: "
                + ", ".join(sorted(overlap))
            )
        meta.update(meta_extra)
    return {
        "ok": not errors,
        "tool_version": TOOL_VERSION,
        "data": data,
        "meta": meta,
        "warnings": warnings or [],
        "commands": commands or [],
        "errors": errors or [],
    }


# --- parser -------------------------------------------------------------

VERBS = ("add", "work", "wait", "status", "list", "show", "retry", "cancel", "prune",
         "output", "capabilities")

# verb -> subparser, rebuilt by build_parser; did_you_mean reads flag tables
# from here so suggestions always come from the parser itself.
_SUBPARSERS: dict[str, _Parser] = {}


def build_parser() -> _Parser:
    parser = _Parser(
        prog="spoolctl",
        description="Local job queue with retries, backoff, and crash recovery.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"spoolctl {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="verb", metavar="VERB", parser_class=_Parser)

    common = _Parser(add_help=False)
    common.add_argument("--db", metavar="PATH", help="queue database path")
    common.add_argument("--json", action="store_true", help="emit the JSON envelope")

    add = sub.add_parser("add", parents=[common], help="enqueue a command")
    add.add_argument("-c", dest="shell_string", metavar="STRING", help="run STRING via sh -c")
    add.add_argument("--key", default=None, metavar="K",
                     help="idempotency key for active queued/running jobs")
    add.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, metavar="SECONDS")
    add.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, metavar="N")
    add.add_argument("argv", nargs=argparse.REMAINDER, metavar="[--] ARGV...")

    work = sub.add_parser("work", parents=[common], help="run jobs until stopped")
    work.add_argument("--once", action="store_true", help="run at most one job, then exit")
    work.add_argument("--drain", action="store_true",
                      help="run until the queue settles (no queued or running jobs), then exit")
    work.add_argument("--poll-interval", type=float, default=None, metavar="SECONDS")
    work.add_argument("--worker-id", default=None, metavar="NAME")

    wait = sub.add_parser("wait", parents=[common],
                          help="block until jobs settle; exit 6 if any failed")
    wait.add_argument("ids", nargs="+", metavar="ID")
    wait.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                      help="give up after SECONDS (exit 4); default: wait forever")
    wait.add_argument("--poll-interval", type=float, default=0.5, metavar="SECONDS")

    status = sub.add_parser("status", parents=[common], help="queue counts and recent dead jobs")
    status.add_argument("--limit", type=int, default=10, metavar="N")

    list_ = sub.add_parser("list", parents=[common], help="enumerate jobs, newest first")
    list_.add_argument("--state", default=None, metavar="CSV",
                       help="comma-separated states to include")
    list_.add_argument("--limit", type=int, default=50, metavar="N",
                       help="max jobs (0 = unlimited)")

    show = sub.add_parser("show", parents=[common], help="one job in full detail")
    show.add_argument("id", metavar="ID")

    retry = sub.add_parser("retry", parents=[common], help="requeue a dead or failed job")
    retry.add_argument("id", metavar="ID")
    retry.add_argument("--force", action="store_true", help="also requeue a running job (unsafe)")

    cancel = sub.add_parser("cancel", parents=[common],
                            help="withdraw a queued job (or stop a running one)")
    cancel.add_argument("id", metavar="ID")
    cancel.add_argument("--running", action="store_true",
                        help="also cancel a running job (its process group is"
                             " killed by the owning worker within a heartbeat)")

    prune = sub.add_parser("prune", parents=[common],
                           help="delete old terminal jobs and their output files")
    prune.add_argument("--older-than", required=True, metavar="DURATION",
                       help="age of finished_at to prune, e.g. 30d, 12h, 90 (seconds)")
    prune.add_argument("--state", default="done", metavar="CSV",
                       help="terminal states to prune (done, dead, canceled); default done")
    prune.add_argument("--dry-run", action="store_true",
                       help="report what would be deleted without deleting")

    output = sub.add_parser("output", parents=[common], help="show a job's captured output")
    output.add_argument("id", metavar="ID")
    output.add_argument("--stream", choices=["stdout", "stderr", "both"], default="both")
    output.add_argument("--raw", action="store_true", help="raw bytes, single stream, no headers")
    output.add_argument("--attempt", type=int, default=None, metavar="N")

    caps = sub.add_parser("capabilities", parents=[common], help="machine-readable contract")

    _SUBPARSERS.clear()
    _SUBPARSERS.update(
        {"add": add, "work": work, "wait": wait, "status": status, "list": list_,
         "show": show, "retry": retry, "cancel": cancel, "prune": prune,
         "output": output, "capabilities": caps}
    )
    return parser


def _flag_candidates(argv: list[str]) -> list[str]:
    flags = {"--help", "--version"}
    if argv and argv[0] in _SUBPARSERS:
        flags.update(
            s for s in _SUBPARSERS[argv[0]]._option_string_actions if s.startswith("--")
        )
    return sorted(flags)


def _parser_exit_to_error(exc: _ParserExit, argv: list[str]) -> CliError:
    """Translate an argparse failure into a contract error with did_you_mean
    sourced from the parser's own verb/flag tables."""
    message = str(exc)
    if "invalid choice" in message and argv:
        bad = argv[0]
        suggestion = _suggest(bad, list(VERBS))
        remediation = (
            f"try: spoolctl {suggestion}" if suggestion else "run: spoolctl --help"
        )
        return CliError(
            "UNKNOWN_COMMAND",
            f"unknown verb: {bad!r}",
            remediation,
            exit_code=EXIT_INPUT,
            did_you_mean=suggestion,
        )
    if message.startswith("unrecognized arguments:"):
        bad = message.split(":", 1)[1].strip().split()[0]
        if bad.startswith("-"):
            suggestion = _suggest(bad, _flag_candidates(argv))
            corrected = [suggestion if t == bad else t for t in argv]
            remediation = (
                "try: spoolctl " + " ".join(corrected)
                if suggestion
                else f"run: spoolctl {argv[0]} --help" if argv else "run: spoolctl --help"
            )
            return CliError(
                "UNKNOWN_FLAG",
                f"unknown flag: {bad}",
                remediation,
                exit_code=EXIT_INPUT,
                did_you_mean=suggestion,
            )
        return CliError(
            "INVALID_INPUT",
            message,
            f"run: spoolctl {argv[0]} --help" if argv else "run: spoolctl --help",
            exit_code=EXIT_INPUT,
        )
    code = "MISSING_REQUIRED" if "required" in message else "INVALID_INPUT"
    return CliError(
        code,
        message,
        f"run: spoolctl {argv[0]} --help" if argv else "run: spoolctl --help",
        exit_code=EXIT_INPUT,
    )


# --- verbs --------------------------------------------------------------

BOTH_ADD_FORMS = "try: spoolctl add -- <cmd> [args...]   or: spoolctl add -c '<shell string>'"


def _open_db(args: argparse.Namespace) -> "sqlite3.Connection":
    return store.connect(store.resolve_db_path(args.db))


def _normalize_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        raise CliError(
            "INVALID_INPUT",
            "--key must not be empty after trimming whitespace",
            "try: spoolctl add --key run-123 -- <cmd>",
        )
    if len(key) > 256:
        raise CliError(
            "INVALID_INPUT",
            f"--key must be <= 256 characters after trimming (got {len(key)})",
            "try a shorter idempotency key",
        )
    if not key.isprintable():
        raise CliError(
            "INVALID_INPUT",
            "--key must contain only printable characters",
            "remove embedded newlines, tabs, or control characters",
        )
    return key


def cmd_add(args: argparse.Namespace) -> VerbResult:
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]

    if args.shell_string is not None and argv:
        joined = shlex.quote(args.shell_string + " " + " ".join(argv))
        raise CliError(
            "INVALID_INPUT",
            "-c takes exactly one string; positional arguments cannot be combined with it",
            f"try: spoolctl add -c {joined}",
        )
    if args.shell_string is not None:
        if not args.shell_string.strip():
            raise CliError("MISSING_REQUIRED", "empty -c command string", BOTH_ADD_FORMS)
        job_argv = ["sh", "-c", args.shell_string]
    elif argv:
        job_argv = argv
    else:
        raise CliError("MISSING_REQUIRED", "no command given", BOTH_ADD_FORMS)

    if args.timeout <= 0:
        raise CliError(
            "INVALID_INPUT",
            f"--timeout must be > 0 (got {args.timeout})",
            "try: spoolctl add --timeout 300 -- <cmd>",
        )
    if args.max_retries < 0:
        raise CliError(
            "INVALID_INPUT",
            f"--max-retries must be >= 0 (got {args.max_retries})",
            "try: spoolctl add --max-retries 3 -- <cmd>",
        )
    key = _normalize_key(args.key)

    conn = _open_db(args)
    try:
        job_id, state, deduplicated = store.add_job_checked(
            conn, job_argv, args.timeout, args.max_retries, time.time(), key)
    finally:
        conn.close()
    if deduplicated:
        human = f"Job {job_id} already active under key '{key}' ({state})"
    else:
        human = f"Added job {job_id}"
    return VerbResult(
        data={"deduplicated": deduplicated, "job_id": job_id, "state": state},
        human=human,
    )


def cmd_work(args: argparse.Namespace) -> VerbResult:
    if args.poll_interval is not None and args.poll_interval <= 0:
        raise CliError(
            "INVALID_INPUT",
            f"--poll-interval must be > 0 (got {args.poll_interval})",
            "try: spoolctl work --poll-interval 1.0",
        )
    if args.drain and args.once:
        raise CliError(
            "INVALID_INPUT",
            "--drain and --once are mutually exclusive",
            "try: spoolctl work --drain   or: spoolctl work --once",
        )
    from spoolctl import worker

    worker_id = args.worker_id or worker.default_worker_id()
    db_path = store.resolve_db_path(args.db)
    if args.once:
        conn = store.connect(db_path)
        try:
            summary = worker.process_one(conn, db_path, worker_id)
        finally:
            conn.close()
        if summary is None:
            return VerbResult(data={"claimed": False}, human="No eligible job")
        data = {"claimed": True, **summary}
        human = (
            f"Job {summary['job_id']} attempt {summary['attempt_no']}"
            f" {summary['result']} -> {summary['job_state'] or 'discarded'}"
        )
        return VerbResult(data=data, human=human)
    poll = args.poll_interval if args.poll_interval is not None else DEFAULT_POLL_INTERVAL
    outcome = worker.work_loop(db_path, worker_id, poll, drain=args.drain)
    if args.drain:
        executed = outcome["executed"]
        human = (
            f"Drained: executed {executed} job(s)" if outcome["drained"]
            else f"Stopped before the queue settled; executed {executed} job(s)"
        )
        return VerbResult(
            data={"drained": outcome["drained"], "executed": executed},
            human=human,
        )
    return VerbResult(data={"stopped": True}, human="", stdout_silent=True)


# --- dispatch -----------------------------------------------------------

# Each handler takes parsed args and returns a VerbResult or raises CliError.
def cmd_status(args: argparse.Namespace) -> VerbResult:
    if args.limit < 0:
        raise CliError(
            "INVALID_INPUT",
            f"--limit must be >= 0 (got {args.limit})",
            "try: spoolctl status --limit 10",
        )
    conn = _open_db(args)
    try:
        counts = store.state_counts(conn)
        dead = store.recent_dead(conn, args.limit)
    finally:
        conn.close()
    lines = ["  ".join(f"{k} {v}" for k, v in counts.items())]
    if dead:
        lines.append("recent dead:")
        for d in dead:
            lines.append(
                f"  #{d['id']} attempts={d['attempts']}"
                f" error={d['last_error'] or '-'} cmd: {d['command']}"
            )
    return VerbResult(
        data={"counts": counts, "recent_dead": dead},
        human="\n".join(lines),
    )


def _parse_states(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    states = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok not in JOB_STATES:
            suggestion = _suggest(tok, list(JOB_STATES))
            valid = ",".join(sorted(JOB_STATES))
            raise CliError(
                "INVALID_INPUT",
                f"unknown state: {tok!r}",
                f"try: spoolctl list --state {suggestion}" if suggestion
                else f"valid states: {valid}",
                did_you_mean=suggestion,
            )
        states.append(tok)
    return states


def cmd_list(args: argparse.Namespace) -> VerbResult:
    states = _parse_states(args.state)
    if args.limit < 0:
        raise CliError(
            "INVALID_INPUT",
            f"--limit must be >= 0 (got {args.limit})",
            "try: spoolctl list --limit 50  (0 = unlimited)",
        )
    conn = _open_db(args)
    try:
        jobs = store.list_jobs(conn, states, args.limit)
    finally:
        conn.close()
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
            "next_run_at": j.next_run_at,
            "note": j.note,
            "started_at": j.started_at,
            "state": j.state,
            "tags": j.tags or {},
            "timeout_seconds": j.timeout_seconds,
        }
        for j in jobs
    ]
    lines = []
    for j in jobs:
        command = " ".join(j.argv)
        if len(command) > 80:
            command = command[:77] + "..."
        lines.append(f"#{j.id}  {j.state}  attempts={j.attempts}  {command}")
    return VerbResult(
        data={"count": len(rows), "jobs": rows},
        human="\n".join(lines) if lines else "No jobs",
    )


def _job_id_arg(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise CliError(
            "INVALID_INPUT",
            f"job id must be an integer (got {raw!r})",
            "try: spoolctl status  (to list job ids)",
        ) from None


def cmd_show(args: argparse.Namespace) -> VerbResult:
    job_id = _job_id_arg(args.id)
    conn = _open_db(args)
    try:
        job = store.get_job(conn, job_id)
        if job is None:
            raise CliError(
                "NOT_FOUND",
                f"no job with id {job_id}",
                "run: spoolctl list  (to see job ids)",
            )
        attempts = store.get_attempts(conn, job_id)
        events = store.get_events(conn, job_id)
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
        "locked_at": job.locked_at,
        "locked_by": job.locked_by,
        "locked_pid": job.locked_pid,
        "max_retries": job.max_retries,
        "next_run_at": job.next_run_at,
        "note": job.note,
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

    command = " ".join(job.argv)
    if len(command) > 80:
        command = command[:77] + "..."
    lines = [f"#{job.id}  {job.state}  attempts={job.attempts}/{job.max_retries}  {command}"]
    if job.idempotency_key:
        lines.append(f"key: {job.idempotency_key}")
    if job.tags:
        tag_text = " ".join(f"{k}={v}" for k, v in sorted(job.tags.items()))
        lines.append(f"tags: {tag_text}")
    if job.note:
        lines.append(f"note: {job.note}")
    for a in attempts:
        exit_part = "-" if a.exit_code is None else str(a.exit_code)
        line = f"  attempt {a.attempt_no}  {a.state}  exit={exit_part}  worker={a.worker_id}"
        if a.error:
            line += f"  error: {a.error}"
        lines.append(line)
    if events:
        lines.append("events:")
        for e in events:
            line = f"  {e['event']}"
            if e["worker_id"]:
                line += f"  worker={e['worker_id']}"
            if e["detail"]:
                line += f"  {e['detail']}"
            lines.append(line)
    return VerbResult(
        data={"attempts": attempt_rows, "events": events, "job": job_data},
        human="\n".join(lines),
    )


def cmd_retry(args: argparse.Namespace) -> VerbResult:
    job_id = _job_id_arg(args.id)
    conn = _open_db(args)
    try:
        outcome, argv = store.retry_job(conn, job_id, args.force, time.time())
    finally:
        conn.close()
    if outcome == "ok":
        return VerbResult(
            data={"job_id": job_id, "state": "queued"},
            human=f"Requeued job {job_id} with a fresh retry budget",
        )
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
    # raced: --force re-check found the row no longer running
    raise CliError(
        "CONFLICT",
        f"job {job_id} changed state before --force could requeue it",
        f"re-check with: spoolctl status, then: spoolctl retry {job_id}",
        exit_code=EXIT_CONFLICT,
    )


_WAIT_TERMINAL = ("canceled", "dead", "done")


def cmd_wait(args: argparse.Namespace) -> VerbResult:
    ids = [_job_id_arg(raw) for raw in args.ids]
    if args.timeout is not None and args.timeout <= 0:
        raise CliError(
            "INVALID_INPUT",
            f"--timeout must be > 0 (got {args.timeout})",
            "try: spoolctl wait --timeout 60 <id...>",
        )
    if args.poll_interval <= 0:
        raise CliError(
            "INVALID_INPUT",
            f"--poll-interval must be > 0 (got {args.poll_interval})",
            "try: spoolctl wait --poll-interval 0.5 <id...>",
        )
    id_list = " ".join(str(i) for i in ids)
    conn = _open_db(args)
    try:
        missing = sorted({i for i in ids if store.get_job(conn, i) is None})
        if missing:
            raise CliError(
                "NOT_FOUND",
                "no job(s) with id(s): " + ", ".join(str(i) for i in missing),
                "run: spoolctl list  (to see job ids)",
            )
        deadline = None if args.timeout is None else time.monotonic() + args.timeout
        while True:
            jobs = {i: store.get_job(conn, i) for i in ids}
            if all(j.state in _WAIT_TERMINAL for j in jobs.values()):
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise CliError(
                    "TIMEOUT",
                    f"jobs not settled after {args.timeout}s",
                    f"retry: spoolctl wait --timeout {args.timeout} {id_list}",
                    exit_code=EXIT_TRANSIENT,
                )
            time.sleep(args.poll_interval)
    finally:
        conn.close()
    all_succeeded = all(j.state == "done" for j in jobs.values())
    data = {
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
    }
    lines = [f"#{i}  {j.state}" for i, j in jobs.items()]
    lines.append("all succeeded" if all_succeeded else "not all succeeded")
    return VerbResult(
        data=data,
        human="\n".join(lines),
        exit_code=EXIT_OK if all_succeeded else EXIT_JOB_FAILURE,
    )


def cmd_cancel(args: argparse.Namespace) -> VerbResult:
    job_id = _job_id_arg(args.id)
    conn = _open_db(args)
    try:
        outcome, state = store.cancel_job(conn, job_id, args.running, time.time())
    finally:
        conn.close()
    if outcome == "ok":
        return VerbResult(
            data={"job_id": job_id, "state": "canceled", "was_running": False},
            human=f"Canceled job {job_id}",
        )
    if outcome == "ok_running":
        return VerbResult(
            data={"job_id": job_id, "state": "canceled", "was_running": True},
            human=f"Canceled job {job_id} (was running; the owning worker kills"
                  " its process group within a heartbeat)",
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
            f" spoolctl cancel --running {job_id}",
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


_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
PRUNABLE_STATES = ("canceled", "dead", "done")


def _parse_duration(raw: str) -> int:
    """DURATION grammar: integer with optional s|m|h|d suffix; bare means
    seconds; 0 matches everything."""
    m = re.fullmatch(r"(\d+)([smhd]?)", raw)
    if m is None:
        raise CliError(
            "INVALID_INPUT",
            f"bad duration: {raw!r} (integer with optional s/m/h/d suffix)",
            "try: spoolctl prune --older-than 30d",
        )
    return int(m.group(1)) * _DURATION_UNITS[m.group(2)]


def _parse_prune_states(raw: str) -> list[str]:
    states = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok not in PRUNABLE_STATES:
            suggestion = _suggest(tok, list(PRUNABLE_STATES))
            raise CliError(
                "INVALID_INPUT",
                f"prune cannot touch state {tok!r}; only terminal states"
                f" ({', '.join(PRUNABLE_STATES)}) may be pruned",
                f"try: spoolctl prune --older-than 30d --state {suggestion}"
                if suggestion else
                f"valid states: {','.join(PRUNABLE_STATES)}",
                did_you_mean=suggestion,
            )
        states.append(tok)
    return states


def cmd_prune(args: argparse.Namespace) -> VerbResult:
    seconds = _parse_duration(args.older_than)
    states = _parse_prune_states(args.state)
    cutoff = time.time() - seconds
    conn = _open_db(args)
    try:
        matches = store.prune_matches(conn, states, cutoff)
        if args.dry_run:
            freed = 0
            for m in matches:
                for paths in m["paths"]:
                    for path in paths:
                        try:
                            freed += os.stat(path).st_size
                        except OSError:
                            pass
            data = {
                "deleted_attempts": sum(m["n_attempts"] for m in matches),
                "deleted_events": sum(m["n_events"] for m in matches),
                "deleted_jobs": len(matches),
                "dry_run": True,
                "freed_bytes": freed,
                "matched": len(matches),
            }
            return VerbResult(
                data=data,
                human=f"would prune {data['deleted_jobs']} job(s),"
                      f" freeing {freed} bytes (dry run)",
            )
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
            conn, [m["job_id"] for m in matches], states, cutoff)
    finally:
        conn.close()
    data = {
        "deleted_attempts": attempts,
        "deleted_events": events,
        "deleted_jobs": jobs,
        "dry_run": False,
        "freed_bytes": freed,
        "matched": len(matches),
    }
    return VerbResult(
        data=data,
        human=f"pruned {jobs} job(s), freed {freed} bytes",
    )


PREVIEW_BYTES = 4096


def _read_stream(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""


def cmd_output(args: argparse.Namespace) -> VerbResult:
    job_id = _job_id_arg(args.id)
    if args.raw and args.json:
        raise CliError(
            "INVALID_INPUT",
            "--raw and --json are mutually exclusive",
            f"try: spoolctl output {job_id} --raw --stream stdout",
        )
    if args.raw and args.stream == "both":
        raise CliError(
            "INVALID_INPUT",
            "--raw needs a single stream",
            f"try: spoolctl output {job_id} --raw --stream stdout",
        )
    conn = _open_db(args)
    try:
        job = store.get_job(conn, job_id)
        if job is None:
            raise CliError(
                "NOT_FOUND",
                f"no job with id {job_id}",
                "run: spoolctl status  (to list job ids)",
            )
        attempts = store.get_attempts(conn, job_id)
    finally:
        conn.close()
    if not attempts:
        return VerbResult(
            data={"attempts": []},
            human=f"Job {job_id} has no attempts yet",
            warnings=[{
                "code": "NO_ATTEMPTS_YET",
                "message": f"job {job_id} has not been executed yet",
            }],
        )
    if args.attempt is not None:
        matching = [a for a in attempts if a.attempt_no == args.attempt]
        if not matching:
            available = ", ".join(str(a.attempt_no) for a in attempts)
            raise CliError(
                "NOT_FOUND",
                f"job {job_id} has no attempt {args.attempt}",
                f"available attempts: {available}",
            )
        attempt = matching[0]
    else:
        attempt = attempts[-1]

    streams = ["stdout", "stderr"] if args.stream == "both" else [args.stream]
    paths = {"stdout": attempt.stdout_path, "stderr": attempt.stderr_path}

    if args.raw:
        sys.stdout.buffer.write(_read_stream(paths[streams[0]]))
        sys.stdout.buffer.flush()
        return VerbResult(data=None, human="", stdout_silent=True)

    if args.json:
        stream_data = {}
        for name in streams:
            blob = _read_stream(paths[name])
            stream_data[name] = {
                "path": paths[name],
                "preview": blob[:PREVIEW_BYTES].decode("utf-8", errors="replace"),
                "preview_truncated": len(blob) > PREVIEW_BYTES,
                "size_bytes": len(blob),
            }
        data = {
            "attempt_no": attempt.attempt_no,
            "attempt_state": attempt.state,
            "attempts_total": len(attempts),
            "job_id": job_id,
            "streams": stream_data,
        }
        return VerbResult(data=data, human="")

    sections = []
    for name in streams:
        body = _read_stream(paths[name]).decode("utf-8", errors="replace")
        sections.append(f"=== job {job_id} attempt {attempt.attempt_no} {name} ===")
        if body:
            sections.append(body.rstrip("\n"))
    return VerbResult(data=None, human="\n".join(sections))


# One-line data-schema summaries per verb; the flags themselves are always
# introspected from the live parser, never hand-maintained.
VERB_SUMMARIES = {
    "add": {
        "summary": "enqueue a command; --key deduplicates active queued/running jobs",
        "data_schema": "{job_id: int, state: 'queued'|'running', deduplicated: bool}",
    },
    "work": {
        "summary": "run jobs until stopped; --once runs at most one;"
                   " --drain runs until the queue settles",
        "data_schema": "--once: {claimed: bool, job_id?, attempt_no?, result?,"
                       " job_state?}; --drain: {drained: bool, executed: int};"
                       " loop mode writes nothing to stdout",
    },
    "wait": {
        "summary": "block until every given job settles (done/dead/canceled);"
                   " exit 0 all done, exit 6 any failed (envelope stays ok:true)",
        "data_schema": "{all_succeeded: bool, jobs: {'<id>': {state, attempts,"
                       " last_exit_code, last_error}}}",
    },
    "status": {
        "summary": "queue counts and recent dead jobs; always exit 0",
        "data_schema": "{counts: {canceled,dead,done,failed,queued,running},"
                       " recent_dead: [{id, command, attempts, last_error,"
                       " finished_at, stdout_path, stderr_path}]}",
    },
    "list": {
        "summary": "enumerate jobs, newest first, optionally filtered by state",
        "data_schema": "{count: int, jobs: [{id, argv, state, attempts,"
                       " max_retries, timeout_seconds, created_at, started_at,"
                       " finished_at, next_run_at, last_exit_code, last_error,"
                       " idempotency_key, tags, note}]}",
    },
    "show": {
        "summary": "one job in full detail: row, attempts, event trail",
        "data_schema": "{job: {id, argv, state, attempts, max_retries,"
                       " timeout_seconds, created_at, started_at, finished_at,"
                       " next_run_at, locked_by, locked_pid, locked_at,"
                       " heartbeat_at, last_exit_code, last_error,"
                       " idempotency_key, tags, note},"
                       " attempts: [{attempt_no, state, worker_id, worker_pid,"
                       " started_at, finished_at, exit_code, error,"
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
                   " then rows; --dry-run reports without deleting",
        "data_schema": "{matched: int, deleted_jobs: int, deleted_attempts:"
                       " int, deleted_events: int, freed_bytes: int,"
                       " dry_run: bool}",
    },
    "output": {
        "summary": "captured stdout/stderr for any attempt of a job",
        "data_schema": "{attempt_no, attempt_state, attempts_total, job_id,"
                       " streams: {stdout|stderr: {path, preview,"
                       " preview_truncated, size_bytes}}} or {attempts: []}",
    },
    "capabilities": {
        "summary": "this machine-readable contract",
        "data_schema": "{attempt_states, contract_policy, contract_version, env,"
                       " error_codes, events, exit_codes, job_states, verbs}",
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


def _describe_verb(name: str, sub: _Parser) -> dict[str, Any]:
    flags = []
    positionals = []
    for action in sub._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if action.option_strings:
            flag = max(action.option_strings, key=len)
            if isinstance(action, argparse._StoreTrueAction):
                ftype = "bool"
                default: Any = False
            else:
                ftype = action.type.__name__ if callable(action.type) else "str"
                default = action.default
            entry: dict[str, Any] = {"default": default, "flag": flag, "type": ftype}
            if action.choices:
                entry["choices"] = sorted(action.choices)
            flags.append(entry)
        else:
            positionals.append({
                "name": action.dest,
                "repeatable": action.nargs == argparse.REMAINDER,
            })
    return {
        "data_schema": VERB_SUMMARIES[name]["data_schema"],
        "flags": sorted(flags, key=lambda f: f["flag"]),
        "positionals": positionals,
        "summary": VERB_SUMMARIES[name]["summary"],
    }


CONTRACT_POLICY = (
    "additive under contract_version 1: consumers must tolerate new verbs,"
    " new enum members (states, events), and new exit codes reachable only"
    " via new verbs; a breaking change to an existing verb's data shape or"
    " exit mapping is what bumps contract_version"
)


def cmd_capabilities(args: argparse.Namespace) -> VerbResult:
    build_parser()  # ensure _SUBPARSERS is populated from the live parser
    verbs = {name: _describe_verb(name, sub) for name, sub in sorted(_SUBPARSERS.items())}
    exit_codes = {
        str(code): dict(sorted(info.items()))
        for code, info in sorted(EXIT_CODES.items())
    }
    data = {
        "attempt_states": sorted(ATTEMPT_STATES),
        "contract_policy": CONTRACT_POLICY,
        "contract_version": CONTRACT_VERSION,
        "env": ENV_DOCS,
        "error_codes": sorted(ERROR_CODES),
        "events": sorted(JOB_EVENT_TYPES),
        "exit_codes": exit_codes,
        "job_states": sorted(JOB_STATES),
        "verbs": verbs,
    }
    lines = [f"spoolctl contract v{CONTRACT_VERSION}"]
    for name, verb in verbs.items():
        lines.append(f"  {name}: {verb['summary']}")
    lines.append("run with --json for the full machine-readable contract")
    return VerbResult(data=data, human="\n".join(lines))


HANDLERS: dict[str, Callable[[argparse.Namespace], VerbResult]] = {
    "add": cmd_add,
    "cancel": cmd_cancel,
    "capabilities": cmd_capabilities,
    "list": cmd_list,
    "output": cmd_output,
    "prune": cmd_prune,
    "retry": cmd_retry,
    "show": cmd_show,
    "status": cmd_status,
    "wait": cmd_wait,
    "work": cmd_work,
}


def _not_implemented(args: argparse.Namespace) -> VerbResult:
    raise CliError(
        "INTERNAL",
        f"verb {args.verb!r} is not implemented in this build",
        "upgrade spoolctl",
        exit_code=EXIT_ENVIRONMENT,
    )


def _json_requested(argv: list[str]) -> bool:
    # Tokens after a standalone "--" belong to the job (add's argv), not us.
    for tok in argv:
        if tok == "--":
            return False
        if tok == "--json":
            return True
    return False


def _emit_failure(err: CliError, json_mode: bool, started: float) -> int:
    if json_mode:
        env = make_envelope(None, started=started, errors=[err.as_dict()])
        print(json.dumps(env, ensure_ascii=False))
    line = f"spoolctl: error: {err.message}"
    if err.did_you_mean:
        line += f" (did you mean {err.did_you_mean!r}?)"
    print(line, file=sys.stderr)
    print(f"  {err.remediation}", file=sys.stderr)
    return err.exit_code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    started = time.monotonic()
    json_mode = _json_requested(argv)
    try:
        parser = build_parser()
        try:
            args = parser.parse_args(argv)
        except _ParserExit as exc:
            raise _parser_exit_to_error(exc, argv) from None
        if args.verb is None:
            parser.print_help()
            return EXIT_OK
        json_mode = getattr(args, "json", json_mode)
        handler = HANDLERS.get(args.verb, _not_implemented)
        result = handler(args)
        if result.stdout_silent:
            return result.exit_code
        if json_mode:
            env = make_envelope(
                result.data,
                started=started,
                warnings=result.warnings,
                commands=result.commands,
                meta_extra=result.meta_extra,
            )
            print(json.dumps(env, ensure_ascii=False))
        else:
            if result.human:
                print(result.human)
            for w in result.warnings:
                print(f"warning: {w.get('message', w.get('code', ''))}", file=sys.stderr)
        return result.exit_code
    except CliError as err:
        return _emit_failure(err, json_mode, started)
    except store.SchemaTooNewError as exc:
        return _emit_failure(
            CliError("INTERNAL", str(exc), "upgrade spoolctl", exit_code=EXIT_ENVIRONMENT),
            json_mode,
            started,
        )
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            return _emit_failure(
                CliError(
                    "LOCKED",
                    "queue database is busy",
                    "retry after a few seconds",
                    exit_code=EXIT_TRANSIENT,
                ),
                json_mode,
                started,
            )
        return _emit_failure(
            CliError("INTERNAL", f"database error: {exc}", "check the queue database",
                     exit_code=EXIT_ENVIRONMENT),
            json_mode,
            started,
        )


if __name__ == "__main__":
    sys.exit(main())
