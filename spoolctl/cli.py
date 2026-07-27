"""argparse dispatch, envelope construction, human rendering, exit codes.

Thin over store/worker. Every verb speaks one machine contract: the
seven-key JSON envelope, the published exit-code dictionary, and errors
that teach the corrected invocation.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import signal
import shlex
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from spoolctl import schemas, store
from spoolctl.errors import CliError
from spoolctl.models import (
    ATTEMPT_STATES,
    CODE_REGISTRY,
    CONTRACT_VERSION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIMEOUT_SECONDS,
    EVENT_LIMIT_MAX,
    ERROR_CODES,
    EXIT_CODES,
    EXIT_CONFLICT,
    EXIT_ENVIRONMENT,
    EXIT_INPUT,
    EXIT_JOB_FAILURE,
    EXIT_OK,
    EXIT_SAFETY,
    EXIT_TRANSIENT,
    FAILURE_REASONS,
    JOB_EVENT_TYPES,
    JOB_STATES,
    HEARTBEAT_INTERVAL,
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
    TAG_FILTER_SCAN_LIMIT,
    TOOL_VERSION,
    VERBS,
    _suggest,
)
from spoolctl.operations import (
    AddInput,
    OutputInput,
    StatusInput,
    WaitInput,
    add_operation,
    output_operation,
    status_operation,
    wait_operation,
)
from spoolctl.validation import (
    _job_id_arg,
    _normalize_key,
    _parse_add_tags,
    _parse_after,
    _parse_at,
    _parse_duration,
    _parse_int_bound,
    _parse_job_cwd,
    _parse_job_env,
    _parse_list_tags,
    _parse_positive_float,
    _parse_priority,
    _parse_prune_states,
    _parse_queue,
    _parse_states,
    _parse_worker_id,
    _validate_positive_float_env,
)

HELP_EPILOG = """\
AGENT/AUTOMATION:
  Run `spoolctl capabilities --json` for the full machine-readable contract:
  verbs, flags, data schemas, exit codes, error codes.
  Run `spoolctl robot-docs guide --json` for workflow guidance.
"""


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


def _int_token(raw: str) -> str:
    return raw


_int_token.__name__ = "int"


def _float_token(raw: str) -> str:
    return raw


_float_token.__name__ = "float"


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
    source_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_epoch is not None:
        try:
            now = datetime.fromtimestamp(float(source_epoch), timezone.utc)
        except (OverflowError, OSError, ValueError):
            now = datetime.now(timezone.utc)
    else:
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

# verb -> subparser, rebuilt by build_parser; did_you_mean reads flag tables
# from here so suggestions always come from the parser itself.
_SUBPARSERS: dict[str, _Parser] = {}


def build_parser() -> _Parser:
    parser = _Parser(
        prog="spoolctl",
        description="Local job queue with retries, backoff, and crash recovery.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"spoolctl {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="verb", metavar="VERB", parser_class=_Parser)

    common_db = _Parser(add_help=False, allow_abbrev=False)
    common_db.add_argument("--db", metavar="PATH", help="queue database path")
    common_db.add_argument("--json", action="store_true", help="emit the JSON envelope")
    common_nodb = _Parser(add_help=False, allow_abbrev=False)
    common_nodb.add_argument("--json", action="store_true", help="emit the JSON envelope")

    add = sub.add_parser("add", parents=[common_db], help="enqueue a command",
                         allow_abbrev=False)
    add.add_argument("-c", dest="shell_string", metavar="STRING", help="run STRING via sh -c")
    add.add_argument("--key", default=None, metavar="K",
                     help="idempotency key for active queued/running jobs")
    add.add_argument("--tag", action="append", default=[], metavar="KEY=VALUE",
                     help="submit-time metadata tag; repeatable")
    add.add_argument("--note", default=None, metavar="STRING",
                     help="submit-time handoff note")
    add.add_argument("--after", default=None, metavar="DURATION",
                     help="run after duration: 30s, 5m, 2h, 1d, or bare seconds")
    add.add_argument("--at", default=None, metavar="TIMESTAMP",
                     help="run at epoch seconds or ISO-8601 timestamp")
    add.add_argument("--priority", default="0", metavar="N",
                     help="claim priority, signed 32-bit integer; higher runs first")
    add.add_argument("--queue", default="default", metavar="NAME",
                     help="lane name, default 'default'")
    add.add_argument("--timeout", type=_int_token, default=DEFAULT_TIMEOUT_SECONDS, metavar="SECONDS")
    add.add_argument("--max-retries", type=_int_token, default=DEFAULT_MAX_RETRIES, metavar="N")
    add.add_argument("--max-crashes", type=_int_token, default=None, metavar="N")
    add.add_argument("--cwd", default=None, metavar="DIR",
                     help="working directory for the job, resolved to absolute at submit")
    add.add_argument("--env", action="append", default=[], metavar="K=V",
                     help="environment override for the job; repeatable")
    add.add_argument("argv", nargs=argparse.REMAINDER, metavar="[--] ARGV...")

    work = sub.add_parser("work", parents=[common_db], help="run jobs until stopped",
                          allow_abbrev=False)
    work.add_argument("--once", action="store_true", help="run at most one job, then exit")
    work.add_argument("--drain", action="store_true",
                      help="run until the queue settles (no queued or running jobs), then exit")
    work.add_argument("--poll-interval", type=_float_token, default=None, metavar="SECONDS")
    work.add_argument("--worker-id", default=None, metavar="NAME")
    work.add_argument("--queue", default="default", metavar="NAME",
                      help="serve one lane; default 'default'")
    work.add_argument("--slots", type=_int_token, default=None, metavar="N",
                      help="optional fleet-wide running-job ceiling for the served lane")

    wait = sub.add_parser("wait", parents=[common_db],
                          help="block until jobs settle; exit 6 if any failed",
                          allow_abbrev=False)
    wait.add_argument("ids", nargs="+", metavar="ID")
    wait.add_argument("--timeout", type=_float_token, default=None, metavar="SECONDS",
                      help="give up after SECONDS (exit 4); default: wait forever")
    wait.add_argument("--poll-interval", type=_float_token, default=0.5, metavar="SECONDS")

    status = sub.add_parser("status", parents=[common_db], help="queue counts and recent dead jobs",
                            allow_abbrev=False)
    status.add_argument("--limit", type=_int_token, default=10, metavar="N")

    list_ = sub.add_parser("list", parents=[common_db], help="enumerate jobs, newest first",
                           allow_abbrev=False)
    list_.add_argument("--state", default=None, metavar="CSV",
                       help="comma-separated states to include")
    list_.add_argument("--tag", action="append", default=[], metavar="KEY[=VALUE]",
                       help="filter by tag existence or exact value; repeatable")
    list_.add_argument("--queue", default=None, metavar="NAME",
                       help="filter to one lane")
    list_.add_argument("--priority-min", default=None, metavar="N",
                       help="minimum priority to include")
    list_.add_argument("--limit", type=_int_token, default=50, metavar="N",
                       help="max jobs (0 = unlimited)")

    show = sub.add_parser("show", parents=[common_db], help="one job in full detail",
                          allow_abbrev=False)
    show.add_argument("id", metavar="ID")

    retry = sub.add_parser("retry", parents=[common_db], help="requeue a dead or failed job",
                           allow_abbrev=False)
    retry.add_argument("id", metavar="ID")
    retry.add_argument("--force", action="store_true", help="also requeue a running job (unsafe)")

    cancel = sub.add_parser("cancel", parents=[common_db],
                            help="withdraw a queued job (or stop a running one)",
                            allow_abbrev=False)
    cancel.add_argument("id", metavar="ID")
    cancel.add_argument("--running", action="store_true",
                        help="also cancel a running job (its process group is"
                             " killed by the owning worker within a heartbeat)")
    cancel.add_argument("-y", "--yes", action="store_true",
                        help="confirm interruption when used with --running")

    prune = sub.add_parser("prune", parents=[common_db],
                           help="delete old terminal jobs and their output files",
                           allow_abbrev=False)
    prune.add_argument("--older-than", required=True, metavar="DURATION",
                       help="age of finished_at to prune, e.g. 30d, 12h, 90 (seconds)")
    prune.add_argument("--state", default="done", metavar="CSV",
                       help="terminal states to prune (done, dead, canceled); default done")
    prune.add_argument("--dry-run", action="store_true",
                       help="report what would be deleted without deleting")
    prune.add_argument("-y", "--yes", action="store_true",
                       help="confirm irreversible deletion")

    output = sub.add_parser("output", parents=[common_db], help="show a job's captured output",
                            allow_abbrev=False)
    output.add_argument("id", metavar="ID")
    output.add_argument("--stream", choices=["stdout", "stderr", "both"], default="both")
    output.add_argument("--raw", action="store_true", help="raw bytes, single stream, no headers")
    output.add_argument("--attempt", type=_int_token, default=None, metavar="N")

    events = sub.add_parser("events", parents=[common_db],
                            help="read or follow the global job event stream",
                            allow_abbrev=False)
    events.add_argument("--job", type=_int_token, default=None, metavar="ID",
                        help="filter to one job id; no existence check is performed")
    events.add_argument("--since-id", "--since-cursor", dest="since_id",
                        type=_int_token, default=None, metavar="N",
                        help="return events with id > N")
    events.add_argument("--limit", type=_int_token, default=None, metavar="N",
                        help="one-shot max events; default 1000, 0 = unlimited")
    events.add_argument("--max-events", type=_int_token, default=None, metavar="N",
                        help="follow mode: stop after N data frames")
    events.add_argument("--idle-timeout", type=_float_token, default=None, metavar="SECONDS",
                        help="follow mode: stop after no new events for SECONDS")
    events.add_argument("--wait", action="store_true",
                        help="long-poll for the next matching event, then return an envelope")
    events.add_argument("--wait-timeout", type=_float_token, default=30.0, metavar="SECONDS",
                        help="budget for --wait; default 30")
    events.add_argument("--follow", action="store_true", help="tail events until interrupted")
    events.add_argument("--poll-interval", type=_float_token, default=0.5, metavar="SECONDS",
                        help="poll rate for --wait/--follow; default 0.5")

    brief = sub.add_parser("brief", parents=[common_nodb], help="compact usage brief",
                           allow_abbrev=False)

    schema = sub.add_parser("schema", parents=[common_nodb], help="export JSON Schemas",
                            allow_abbrev=False)
    schema.add_argument("--verb", dest="schema_verb", default=None, metavar="NAME",
                        help="export only one verb data schema")

    caps = sub.add_parser("capabilities", parents=[common_nodb], help="machine-readable contract",
                          allow_abbrev=False)
    robot = sub.add_parser("robot-docs", parents=[common_nodb], help="agent workflow guide",
                           allow_abbrev=False)
    robot_sub = robot.add_subparsers(dest="robot_docs_command", metavar="COMMAND",
                                     parser_class=_Parser, required=True)
    guide = robot_sub.add_parser("guide", parents=[common_nodb],
                                 help="agent workflow handbook", allow_abbrev=False)

    _SUBPARSERS.clear()
    _SUBPARSERS.update(
        {"add": add, "work": work, "wait": wait, "status": status, "list": list_,
         "show": show, "retry": retry, "cancel": cancel, "prune": prune,
         "output": output, "events": events, "brief": brief, "schema": schema,
         "capabilities": caps, "robot-docs": robot}
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
    if message.startswith("argument VERB:") and "invalid choice" in message and argv:
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
    if message.startswith("argument ") and "invalid choice" in message:
        return CliError(
            "INVALID_INPUT",
            message,
            f"run: spoolctl {argv[0]} --help" if argv else "run: spoolctl --help",
            exit_code=EXIT_INPUT,
        )
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
    db_path = store.resolve_db_path(args.db)
    try:
        return store.connect(db_path)
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
                f"cannot open queue database at {db_path!r}: {exc.strerror or exc}",
                "choose a writable database path with --db or SPOOLCTL_DB",
            ) from None
        raise
    except sqlite3.OperationalError as exc:
        text = str(exc)
        if "unable to open database file" in text or "readonly" in text:
            raise CliError(
                "INVALID_INPUT",
                f"cannot open queue database at {db_path!r}: {text}",
                "choose a writable database path with --db or SPOOLCTL_DB",
            ) from None
        raise


def _tags_match(tags: dict[str, str], predicates: list[tuple[str, str | None]]) -> bool:
    for key, value in predicates:
        if key not in tags:
            return False
        if value is not None and tags[key] != value:
            return False
    return True


def cmd_add(args: argparse.Namespace) -> VerbResult:
    argv = list(args.argv)
    explicit_boundary = getattr(args, "_explicit_command_boundary", False)
    if argv and argv[0] == "--":
        explicit_boundary = True
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
        if not explicit_boundary and any(tok.startswith("-") for tok in argv):
            raise CliError(
                "INVALID_INPUT",
                "flag-looking token in add command argv without explicit -- boundary",
                "try: spoolctl add [spoolctl flags] -- <cmd> [args...]",
            )
        job_argv = argv
    else:
        raise CliError("MISSING_REQUIRED", "no command given", BOTH_ADD_FORMS)

    timeout = _parse_int_bound(args.timeout, flag="--timeout", minimum=1)
    max_retries = _parse_int_bound(args.max_retries, flag="--max-retries", minimum=0)
    max_crashes = (
        _parse_int_bound(args.max_crashes, flag="--max-crashes", minimum=0)
        if args.max_crashes is not None else None
    )
    if timeout <= 0:
        raise CliError(
            "INVALID_INPUT",
            f"--timeout must be > 0 (got {timeout})",
            "try: spoolctl add --timeout 300 -- <cmd>",
        )
    if max_retries < 0:
        raise CliError(
            "INVALID_INPUT",
            f"--max-retries must be >= 0 (got {max_retries})",
            "try: spoolctl add --max-retries 3 -- <cmd>",
        )
    if max_crashes is not None and max_crashes < 0:
        raise CliError(
            "INVALID_INPUT",
            f"--max-crashes must be >= 0 (got {max_crashes})",
            "try: spoolctl add --max-crashes 3 -- <cmd>",
        )
    if args.after is not None and args.at is not None:
        raise CliError(
            "INVALID_INPUT",
            "--after and --at are mutually exclusive",
            "try: spoolctl add --after 30s -- <cmd>   or: spoolctl add --at 2026-07-16T09:00:00-04:00 -- <cmd>",
        )
    priority = _parse_priority(args.priority)
    queue = _parse_queue(args.queue)
    key = _normalize_key(args.key)
    tags = _parse_add_tags(args.tag)
    cwd = _parse_job_cwd(args.cwd)
    env = _parse_job_env(args.env)
    if args.note is not None and len(args.note) > 10000:
        raise CliError(
            "INVALID_INPUT",
            f"--note must be <= 10000 characters (got {len(args.note)})",
            "use a shorter note",
        )

    now = time.time()
    next_run_at = None
    if args.after is not None:
        next_run_at = now + _parse_after(args.after)
    elif args.at is not None:
        next_run_at = max(_parse_at(args.at), now)

    result = add_operation(
        AddInput(
            db_path=args.db,
            argv=job_argv,
            timeout=timeout,
            max_retries=max_retries,
            max_crashes=max_crashes,
            now=now,
            key=key,
            tags=tags,
            note=args.note,
            priority=priority,
            queue=queue,
            next_run_at=next_run_at,
            cwd=cwd,
            env=env,
        )
    )
    job_id = result.data["job_id"]
    if result.deduplicated:
        human = f"Job {job_id} already active under key '{key}' ({result.state})"
    else:
        human = f"Added job {job_id}"
    return VerbResult(
        data=result.data,
        human=human,
        warnings=result.warnings,
    )


def cmd_work(args: argparse.Namespace) -> VerbResult:
    _validate_positive_float_env("SPOOLCTL_TEST_HEARTBEAT_INTERVAL")
    _validate_positive_float_env("SPOOLCTL_TEST_REAP_THRESHOLD")
    poll_interval = (
        _parse_positive_float(
            args.poll_interval, flag="--poll-interval", maximum=MAX_POLL_INTERVAL_SECONDS
        )
        if args.poll_interval is not None else None
    )
    if args.drain and args.once:
        raise CliError(
            "INVALID_INPUT",
            "--drain and --once are mutually exclusive",
            "try: spoolctl work --drain   or: spoolctl work --once",
        )
    lane = _parse_queue(args.queue)
    slots = (
        _parse_int_bound(args.slots, flag="--slots", minimum=1)
        if args.slots is not None else None
    )
    from spoolctl import worker

    worker_id = _parse_worker_id(args.worker_id) or worker.default_worker_id()
    db_path = store.resolve_db_path(args.db)
    if args.once:
        conn = store.connect(db_path)
        try:
            summary = worker.process_one(conn, db_path, worker_id, lane=lane, slots=slots)
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
    poll = poll_interval if poll_interval is not None else DEFAULT_POLL_INTERVAL
    outcome = worker.work_loop(db_path, worker_id, poll, drain=args.drain,
                               lane=lane, slots=slots)
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
    limit = _parse_int_bound(
        args.limit, flag="--limit", minimum=0, maximum=STATUS_LIMIT_MAX
    )
    data = status_operation(StatusInput(db_path=args.db, limit=limit))
    counts = data["counts"]
    scheduled = data["scheduled"]
    queues = data["queues"]
    dead = data["recent_dead"]
    lines = ["  ".join(f"{k} {v}" for k, v in counts.items())]
    if scheduled > 0:
        lines.append(f"scheduled {scheduled}")
    non_default_queues = {
        name: data for name, data in queues.items() if name != "default"
    }
    if non_default_queues:
        lines.append("queues:")
        for name, queue_data in queues.items():
            counts_text = "  ".join(f"{k} {v}" for k, v in queue_data["counts"].items())
            suffix = (
                f"  scheduled {queue_data['scheduled']}"
                if queue_data["scheduled"] > 0 else ""
            )
            lines.append(f"  {name}: {counts_text}{suffix}")
    if dead:
        lines.append("recent dead:")
        for d in dead:
            crash_text = f" crashes={d['crashes']}" if d.get("crashes") else ""
            lines.append(
                f"  #{d['id']} attempts={d['attempts']}"
                f"{crash_text} error={d['last_error'] or '-'} cmd: {d['command']}"
            )
    return VerbResult(
        data=data,
        human="\n".join(lines),
    )


def cmd_list(args: argparse.Namespace) -> VerbResult:
    states = _parse_states(args.state)
    tag_predicates = _parse_list_tags(args.tag)
    queue = _parse_queue(args.queue) if args.queue is not None else None
    priority_min = (
        _parse_priority(args.priority_min, flag="--priority-min")
        if args.priority_min is not None else None
    )
    limit = _parse_int_bound(
        args.limit, flag="--limit", minimum=0, maximum=LIST_LIMIT_MAX
    )
    effective_limit = LIST_LIMIT_MAX if limit == 0 else limit
    fetch_limit = (
        TAG_FILTER_SCAN_LIMIT + 1 if tag_predicates else effective_limit + 1
    )
    conn = _open_db(args)
    try:
        jobs = store.list_jobs(
            conn, states, fetch_limit,
            queue=queue, priority_min=priority_min,
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
    lines = []
    for j in jobs:
        command = " ".join(j.argv)
        if len(command) > 80:
            command = command[:77] + "..."
        extra = ""
        if j.crashes:
            extra += f"  crashes={j.crashes}"
        if j.cwd:
            extra += f"  cwd={j.cwd}"
        lines.append(
            f"#{j.id}  {j.state}  queue={j.queue}  priority={j.priority}"
            f"  next_run_at={j.next_run_at:g}  attempts={j.attempts}{extra}  {command}"
        )
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
    return VerbResult(
        data={"count": len(rows), "jobs": rows},
        human="\n".join(lines) if lines else "No jobs",
        warnings=warnings,
        meta_extra={"pagination": pagination},
    )


def current_job_failure_reason(job: store.Job, attempts: list[store.Attempt]) -> str | None:
    if job.state in {"done", "queued", "running"}:
        return None
    for attempt in sorted(attempts, key=lambda a: a.attempt_no, reverse=True):
        if attempt.state != "succeeded" and attempt.failure_reason is not None:
            return attempt.failure_reason
    return None


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

    command = " ".join(job.argv)
    if len(command) > 80:
        command = command[:77] + "..."
    failure_retries_used = job.attempts - job.crashes
    max_crashes = (
        "unbounded" if job.max_crashes is None else str(job.max_crashes)
    )
    lines = [
        f"#{job.id}  {job.state}  failure retries used: "
        f"{failure_retries_used}/{job.max_retries}  worker crashes: "
        f"{job.crashes} (tolerated before dead: {max_crashes})  {command}"
    ]
    lines.append(f"queue: {job.queue}")
    lines.append(f"priority: {job.priority}")
    lines.append(f"next_run_at: {job.next_run_at:g}")
    lines.append(f"cwd: {job.cwd or 'inherit'}")
    if job.env:
        env_text = " ".join(f"{k}={v}" for k, v in sorted(job.env.items()))
        lines.append(f"env: {env_text}")
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


def cmd_wait(args: argparse.Namespace) -> VerbResult:
    ids = [_job_id_arg(raw) for raw in args.ids]
    timeout = (
        _parse_positive_float(args.timeout, flag="--timeout", maximum=MAX_WAIT_SECONDS)
        if args.timeout is not None else None
    )
    poll_interval = _parse_positive_float(
        args.poll_interval, flag="--poll-interval", maximum=MAX_POLL_INTERVAL_SECONDS
    )
    result = wait_operation(
        WaitInput(
            db_path=args.db,
            ids=ids,
            timeout=timeout,
            poll_interval=poll_interval,
        )
    )
    lines = [f"#{i}  {result.data['jobs'][str(i)]['state']}" for i in ids]
    lines.append("all succeeded" if result.all_succeeded else "not all succeeded")
    return VerbResult(
        data=result.data,
        human="\n".join(lines),
        exit_code=EXIT_OK if result.all_succeeded else EXIT_JOB_FAILURE,
    )


def cmd_cancel(args: argparse.Namespace) -> VerbResult:
    job_id = _job_id_arg(args.id)
    if args.running and not args.yes:
        raise CliError(
            "SAFETY_BLOCK",
            f"job {job_id} may be running; canceling a running job kills its process",
            f"preview state with: spoolctl show {job_id}; confirm with:"
            f" spoolctl cancel --running --yes {job_id}",
            exit_code=EXIT_SAFETY,
        )
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


def cmd_prune(args: argparse.Namespace) -> VerbResult:
    seconds = _parse_duration(args.older_than)
    states = _parse_prune_states(args.state)
    if args.yes and args.dry_run:
        raise CliError(
            "INVALID_INPUT",
            "--yes and --dry-run are mutually exclusive",
            "preview with: spoolctl prune --older-than 30d --dry-run; confirm with: spoolctl prune --older-than 30d --yes",
        )
    if not args.yes and not args.dry_run:
        raise CliError(
            "SAFETY_BLOCK",
            "prune deletes jobs, attempts, events, and captured output files",
            "preview with: spoolctl prune --older-than 30d --dry-run; confirm with: spoolctl prune --older-than 30d --yes",
            exit_code=EXIT_SAFETY,
        )
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
                "actual": False,
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
        "actual": True,
        "dry_run": False,
        "freed_bytes": freed,
        "irreversible": True,
        "matched": len(matches),
    }
    return VerbResult(
        data=data,
        human=f"pruned {jobs} job(s), freed {freed} bytes",
    )


def _validate_events_args(args: argparse.Namespace) -> None:
    args.job = (
        _parse_int_bound(args.job, flag="--job", minimum=1)
        if args.job is not None else None
    )
    args.since_id = (
        _parse_int_bound(args.since_id, flag="--since-id", minimum=0)
        if args.since_id is not None else None
    )
    args.limit = (
        _parse_int_bound(args.limit, flag="--limit", minimum=0, maximum=EVENT_LIMIT_MAX)
        if args.limit is not None else None
    )
    args.max_events = (
        _parse_int_bound(
            args.max_events, flag="--max-events", minimum=1, maximum=EVENT_LIMIT_MAX
        )
        if args.max_events is not None else None
    )
    args.idle_timeout = (
        _parse_positive_float(args.idle_timeout, flag="--idle-timeout", maximum=MAX_WAIT_SECONDS)
        if args.idle_timeout is not None else None
    )
    args.poll_interval = _parse_positive_float(
        args.poll_interval, flag="--poll-interval", maximum=MAX_POLL_INTERVAL_SECONDS
    )
    args.wait_timeout = _parse_positive_float(
        args.wait_timeout, flag="--wait-timeout", maximum=MAX_WAIT_SECONDS
    )
    if args.job is not None and args.job <= 0:
        raise CliError(
            "INVALID_INPUT",
            f"--job must be a positive integer (got {args.job})",
            "try: spoolctl events --job 1 --json",
        )
    if args.since_id is not None and args.since_id < 0:
        raise CliError(
            "INVALID_INPUT",
            f"--since-id must be >= 0 (got {args.since_id})",
            "try: spoolctl events --since-id 0 --json",
        )
    if args.limit is not None and args.limit < 0:
        raise CliError(
            "INVALID_INPUT",
            f"--limit must be >= 0 (got {args.limit})",
            "try: spoolctl events --limit 1000 --json",
        )
    if args.poll_interval <= 0:
        raise CliError(
            "INVALID_INPUT",
            f"--poll-interval must be > 0 (got {args.poll_interval})",
            "try: spoolctl events --wait --poll-interval 0.5 --json",
        )
    if args.wait_timeout <= 0:
        raise CliError(
            "INVALID_INPUT",
            f"--wait-timeout must be > 0 (got {args.wait_timeout})",
            "try: spoolctl events --wait --wait-timeout 30 --json",
        )
    if args.follow and args.wait:
        raise CliError(
            "INVALID_INPUT",
            "--wait and --follow are mutually exclusive",
            "use --wait for one envelope response, or --follow for a stream",
        )
    if args.follow and args.limit is not None:
        raise CliError(
            "INVALID_INPUT",
            "--limit cannot be used with --follow",
            "use --max-events for bounded follow, or omit --follow",
        )
    if not args.follow and args.max_events is not None:
        raise CliError(
            "INVALID_INPUT",
            "--max-events is only valid with --follow",
            "use --limit for one-shot events, or add --follow",
        )
    if not args.follow and args.idle_timeout is not None:
        raise CliError(
            "INVALID_INPUT",
            "--idle-timeout is only valid with --follow",
            "use --wait --wait-timeout for one-shot long-poll, or add --follow",
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


def _format_event_line(event: dict) -> str:
    ts = datetime.fromtimestamp(event["at"], timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    ts = ts[:-4] + "Z"
    worker_id = event["worker_id"] or "-"
    detail = event["detail"] or "-"
    return (
        f"{event['id']}  {ts} #{event['job_id']} {event['event']}"
        f"  {worker_id}  {detail}"
    )


def _write_event(event: dict, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(event, ensure_ascii=False), flush=True)
    else:
        print(_format_event_line(event), flush=True)


def _write_control_frame(frame_type: str, reason: str, **extra: Any) -> None:
    control = {"type": frame_type, "reason": reason}
    control.update(extra)
    print(json.dumps({"control": control}, ensure_ascii=False), flush=True)


def _run_events_follow(args: argparse.Namespace) -> VerbResult:
    conn = _open_db(args)
    stop = False
    emitted = 0
    idle_started = time.monotonic()

    def on_signal(signum, frame):
        nonlocal stop
        stop = True

    old_int = signal.signal(signal.SIGINT, on_signal)
    old_term = signal.signal(signal.SIGTERM, on_signal)
    try:
        cursor = (
            args.since_id
            if args.since_id is not None
            else store.event_high_water(conn)
        )
        while not stop:
            rows = store.list_events(conn, cursor, args.job, 0)
            if not rows:
                if (
                    args.idle_timeout is not None
                    and time.monotonic() - idle_started >= args.idle_timeout
                ):
                    if args.json:
                        _write_control_frame("end", "idle_timeout")
                    break
                time.sleep(args.poll_interval)
                continue
            for event in rows:
                if stop:
                    break
                _write_event(event, args.json)
                emitted += 1
                cursor = event["id"]
                idle_started = time.monotonic()
                if args.max_events is not None and emitted >= args.max_events:
                    if args.json:
                        _write_control_frame("end", "max_events")
                    stop = True
                    break
    except sqlite3.Error as exc:
        message = f"event follow failed: {exc}"
        if args.json:
            _write_control_frame("error", "sqlite_error", message=message, exit_code=EXIT_ENVIRONMENT)
        print(f"spoolctl: error: {message}", file=sys.stderr)
        return VerbResult(data=None, human="", stdout_silent=True, exit_code=EXIT_ENVIRONMENT)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        conn.close()
    return VerbResult(data=None, human="", stdout_silent=True)


def cmd_events(args: argparse.Namespace) -> VerbResult:
    _validate_events_args(args)
    limit = 1000 if args.limit is None else args.limit
    if args.follow:
        return _run_events_follow(args)

    since_id = 0 if args.since_id is None else args.since_id
    conn = _open_db(args)
    try:
        waited_start = time.monotonic()
        wait_reason = None
        if args.wait:
            deadline = waited_start + args.wait_timeout
            while True:
                probe = store.list_events(conn, since_id, args.job, 1)
                if probe:
                    wait_reason = "records_available"
                    break
                if time.monotonic() >= deadline:
                    wait_reason = "timeout"
                    break
                time.sleep(args.poll_interval)
        events, pagination = _event_page(conn, since_id, args.job, limit)
    finally:
        conn.close()

    meta_extra: dict[str, Any] = {"pagination": pagination}
    if args.wait:
        meta_extra["wait"] = {
            "reason": wait_reason,
            "waited_ms": int((time.monotonic() - waited_start) * 1000),
        }
    human = "\n".join(_format_event_line(e) for e in events) if events else "No events"
    return VerbResult(
        data={"count": len(events), "events": events},
        human=human,
        meta_extra=meta_extra,
    )


def cmd_output(args: argparse.Namespace) -> VerbResult:
    job_id = _job_id_arg(args.id)
    attempt_no = (
        _parse_int_bound(args.attempt, flag="--attempt", minimum=1)
        if args.attempt is not None else None
    )
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
    result = output_operation(
        OutputInput(
            db_path=args.db,
            job_id=job_id,
            attempt_no=attempt_no,
            stream=args.stream,
        )
    )
    if result.warnings:
        return VerbResult(
            data=result.data,
            human=f"Job {job_id} has no attempts yet",
            warnings=result.warnings,
        )

    assert result.data is not None
    streams = list(result.stream_bytes)
    attempt_no = result.data["attempt_no"]

    if args.raw:
        sys.stdout.buffer.write(result.stream_bytes[streams[0]])
        sys.stdout.buffer.flush()
        return VerbResult(data=None, human="", stdout_silent=True)

    if args.json:
        return VerbResult(data=result.data, human="")

    sections = []
    for name in streams:
        body = result.stream_bytes[name].decode("utf-8", errors="replace")
        sections.append(f"=== job {job_id} attempt {attempt_no} {name} ===")
        if body:
            sections.append(body.rstrip("\n"))
    return VerbResult(data=None, human="\n".join(sections))


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


def _describe_verb(name: str, sub: _Parser) -> dict[str, Any]:
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


def cmd_robot_docs(args: argparse.Namespace) -> VerbResult:
    if args.robot_docs_command != "guide":
        raise CliError(
            "MISSING_REQUIRED",
            "missing robot-docs subcommand",
            "run: spoolctl robot-docs guide --json",
        )
    text, tokens = _robot_docs_text()
    return VerbResult(
        data={
            "approx_tokens": tokens,
            "sections": ROBOT_DOC_SECTIONS,
            "text": text,
        },
        human=text,
    )


def cmd_brief(args: argparse.Namespace) -> VerbResult:
    text, tokens = schemas.build_brief(VERB_SUMMARIES, EXIT_CODES, JOB_STATES, ENV_DOCS)
    return VerbResult(
        data={
            "approx_tokens": tokens,
            "budget_tokens": schemas.BRIEF_BUDGET_TOKENS,
            "text": text,
        },
        human=text,
    )


def cmd_schema(args: argparse.Namespace) -> VerbResult:
    verb_names = sorted(schemas.VERB_SCHEMAS)
    if args.schema_verb is None:
        selected = {name: schemas.VERB_SCHEMAS[name] for name in verb_names}
    else:
        if args.schema_verb not in schemas.VERB_SCHEMAS:
            suggestion = _suggest(args.schema_verb, verb_names)
            raise CliError(
                "INVALID_INPUT",
                f"unknown schema verb: {args.schema_verb!r}",
                f"valid verbs: {', '.join(verb_names)}",
                did_you_mean=suggestion,
            )
        selected = {args.schema_verb: schemas.VERB_SCHEMAS[args.schema_verb]}
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
    return VerbResult(data=data, human="\n".join(lines))


def cmd_capabilities(args: argparse.Namespace) -> VerbResult:
    build_parser()  # ensure _SUBPARSERS is populated from the live parser
    verbs = {name: _describe_verb(name, sub) for name, sub in sorted(_SUBPARSERS.items())}
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
    return VerbResult(data=data, human="\n".join(lines))


HANDLERS: dict[str, Callable[[argparse.Namespace], VerbResult]] = {
    "add": cmd_add,
    "brief": cmd_brief,
    "cancel": cmd_cancel,
    "capabilities": cmd_capabilities,
    "events": cmd_events,
    "list": cmd_list,
    "output": cmd_output,
    "prune": cmd_prune,
    "retry": cmd_retry,
    "robot-docs": cmd_robot_docs,
    "schema": cmd_schema,
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
        if tok == "--json" or tok.startswith("--json="):
            return True
    return False


def _explicit_add_boundary(argv: list[str]) -> bool:
    try:
        add_index = argv.index("add")
    except ValueError:
        return False
    return "--" in argv[add_index + 1:]


def _verbless_error(argv: list[str]) -> CliError | None:
    if not argv:
        return CliError(
            "MISSING_REQUIRED",
            "missing required verb",
            "run: spoolctl --help",
            exit_code=EXIT_INPUT,
        )
    if argv in (["--help"], ["-h"], ["--version"]):
        return None
    if any(tok in VERBS for tok in argv):
        return None
    for tok in argv:
        if tok.startswith("-") and tok not in {"--json"} and not tok.startswith("--json="):
            suggestion = _suggest(tok, ["--help", "--version", "--json"])
            return CliError(
                "UNKNOWN_FLAG",
                f"unknown flag: {tok}",
                "run: spoolctl --help",
                exit_code=EXIT_INPUT,
                did_you_mean=suggestion,
            )
    if all(tok == "--json" or tok.startswith("--json=") for tok in argv):
        return CliError(
            "MISSING_REQUIRED",
            "missing required verb",
            "run: spoolctl --help",
            exit_code=EXIT_INPUT,
        )
    return None


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
        verbless = _verbless_error(argv)
        if verbless is not None:
            return _emit_failure(verbless, json_mode, started)
        parser = build_parser()
        try:
            args = parser.parse_args(argv)
        except _ParserExit as exc:
            raise _parser_exit_to_error(exc, argv) from None
        if args.verb is None:
            raise CliError(
                "MISSING_REQUIRED",
                "missing required verb",
                "run: spoolctl --help",
                exit_code=EXIT_INPUT,
            )
        json_mode = getattr(args, "json", json_mode)
        if args.verb == "add":
            args._explicit_command_boundary = _explicit_add_boundary(argv)
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
    except Exception as exc:
        return _emit_failure(
            CliError(
                "INTERNAL",
                f"unexpected internal error: {exc}",
                "retry with --json and file a bug if the problem persists",
                exit_code=EXIT_ENVIRONMENT,
            ),
            json_mode,
            started,
        )


if __name__ == "__main__":
    sys.exit(main())
