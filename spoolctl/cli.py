"""Argparse adapter, envelope emission, rendering, and exit-code mapping.

This module owns parser construction, command dispatch, stdout/stderr behavior,
and CLI-specific diagnostics. Reusable command bodies and contract metadata live
in sibling modules.
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

from spoolctl import store
from spoolctl.errors import CliError
from spoolctl.contract import (
    build_brief,
    build_capabilities,
    build_robot_docs,
    build_schema,
)
from spoolctl.models import (
    CONTRACT_VERSION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIMEOUT_SECONDS,
    EVENT_LIMIT_MAX,
    EXIT_CONFLICT,
    EXIT_ENVIRONMENT,
    EXIT_INPUT,
    EXIT_JOB_FAILURE,
    EXIT_OK,
    EXIT_SAFETY,
    EXIT_TRANSIENT,
    LIST_LIMIT_MAX,
    MAX_POLL_INTERVAL_SECONDS,
    MAX_WAIT_SECONDS,
    STATUS_LIMIT_MAX,
    TAG_FILTER_SCAN_LIMIT,
    TOOL_VERSION,
    VERBS,
    _suggest,
)
from spoolctl.operations import (
    AddInput,
    ConfigShowInput,
    ConfigValidateInput,
    DoctorInput,
    EventsInput,
    ListInput,
    OutputInput,
    ShowInput,
    StatusInput,
    WaitInput,
    add_operation,
    config_show_operation,
    config_validate_operation,
    doctor_operation,
    events_operation,
    list_operation,
    output_operation,
    show_operation,
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
  Contract: `spoolctl capabilities --json`; guide: `spoolctl robot-docs guide --json`.
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

    config_show = sub.add_parser("config-show", parents=[common_db],
                                 help="show effective read-only configuration",
                                 allow_abbrev=False)

    config_validate = sub.add_parser("config-validate", parents=[common_nodb],
                                     help="validate project config",
                                     allow_abbrev=False)
    config_validate.add_argument("path", nargs="?", metavar="PATH",
                                 help="config file to validate; defaults to .spoolctl/config.json")

    doctor = sub.add_parser("doctor", parents=[common_db],
                            help="check local readiness without repairs",
                            allow_abbrev=False)

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
         "output": output, "events": events, "config-show": config_show,
         "config-validate": config_validate, "doctor": doctor, "brief": brief, "schema": schema,
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
    db_path = store.resolve_db_path(args.db, base_dir=os.getcwd())
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
            base_dir=os.getcwd(),
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
    db_path = store.resolve_db_path(args.db, base_dir=os.getcwd())
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
    data = status_operation(StatusInput(db_path=args.db, limit=limit, base_dir=os.getcwd()))
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
    result = list_operation(
        ListInput(
            db_path=args.db,
            states=states,
            tag_predicates=tag_predicates,
            queue=queue,
            priority_min=priority_min,
            effective_limit=effective_limit,
            base_dir=os.getcwd(),
        )
    )
    lines = []
    for j in result.jobs:
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
    return VerbResult(
        data=result.data,
        human="\n".join(lines) if lines else "No jobs",
        warnings=result.warnings,
        meta_extra={"pagination": result.pagination},
    )


def cmd_show(args: argparse.Namespace) -> VerbResult:
    job_id = _job_id_arg(args.id)
    result = show_operation(
        ShowInput(db_path=args.db, job_id=job_id, base_dir=os.getcwd())
    )
    job = result.job
    attempts = result.attempts
    events = result.events

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
        data=result.data,
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
            base_dir=os.getcwd(),
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
    result = events_operation(
        EventsInput(
            db_path=args.db,
            since_id=since_id,
            job_id=args.job,
            limit=limit,
            wait=args.wait,
            wait_timeout=args.wait_timeout,
            poll_interval=args.poll_interval,
            base_dir=os.getcwd(),
        )
    )
    meta_extra: dict[str, Any] = {"pagination": result.pagination}
    if result.wait is not None:
        meta_extra["wait"] = result.wait
    events = result.events
    human = "\n".join(_format_event_line(e) for e in events) if events else "No events"
    return VerbResult(
        data=result.data,
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
            base_dir=os.getcwd(),
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


def cmd_config_show(args: argparse.Namespace) -> VerbResult:
    data = config_show_operation(
        ConfigShowInput(db_path=args.db, base_dir=os.getcwd())
    )
    human = "\n".join([
        f"config_path: {data['config_path']}",
        f"config_exists: {str(data['config_exists']).lower()}",
        f"config_valid: {str(data['config_valid']).lower()}",
        f"db_path: {data['values']['db_path']}",
        f"db_source: {data['sources']['db_path']}",
    ])
    return VerbResult(data=data, human=human)


def cmd_config_validate(args: argparse.Namespace) -> VerbResult:
    data = config_validate_operation(
        ConfigValidateInput(path=args.path, base_dir=os.getcwd())
    )
    human = "\n".join([
        f"config_path: {data['config_path']}",
        f"exists: {str(data['exists']).lower()}",
        f"valid: {str(data['valid']).lower()}",
        f"format: {data['format']}",
        f"schema_version: {data['schema_version']}",
    ])
    return VerbResult(data=data, human=human)


def _format_doctor_human(data: dict[str, Any]) -> str:
    ready = "yes" if data["ready"] else "no"
    config = data["config"]
    db_path = config.get("db_path") or "-"
    db_source = config.get("db_source") or "-"
    summary = data["summary"]
    lines = [
        f"ready: {ready}",
        f"db: {db_path} ({db_source})",
        f"schema: {data['versions']['schema_version']}",
        "checks: "
        f"{summary['passed']} passed, {summary['warnings']} warnings, "
        f"{summary['failed']} failed, {summary['skipped']} skipped",
    ]
    failures = [c for c in data["checks"] if c["status"] == "fail"]
    if failures:
        lines.append("failed checks:")
        for check in failures:
            lines.append(f"  {check['id']}: {check['message']}")
            if check.get("remediation"):
                lines.append(f"    remediation: {check['remediation']}")
    return "\n".join(lines)


def cmd_doctor(args: argparse.Namespace) -> VerbResult:
    data = doctor_operation(
        DoctorInput(
            db_path=args.db,
            base_dir=os.getcwd(),
            parser_verbs=dict(_SUBPARSERS),
        )
    )
    return VerbResult(
        data=data,
        human=_format_doctor_human(data),
        exit_code=EXIT_OK if data["ready"] else EXIT_ENVIRONMENT,
    )


def cmd_robot_docs(args: argparse.Namespace) -> VerbResult:
    if args.robot_docs_command != "guide":
        raise CliError(
            "MISSING_REQUIRED",
            "missing robot-docs subcommand",
            "run: spoolctl robot-docs guide --json",
        )
    data, human = build_robot_docs()
    return VerbResult(data=data, human=human)


def cmd_brief(args: argparse.Namespace) -> VerbResult:
    data, human = build_brief()
    return VerbResult(data=data, human=human)


def cmd_schema(args: argparse.Namespace) -> VerbResult:
    data, human = build_schema(args.schema_verb)
    return VerbResult(data=data, human=human)


def cmd_capabilities(args: argparse.Namespace) -> VerbResult:
    build_parser()  # ensure _SUBPARSERS is populated from the live parser
    data, human = build_capabilities(_SUBPARSERS)
    return VerbResult(data=data, human=human)


HANDLERS: dict[str, Callable[[argparse.Namespace], VerbResult]] = {
    "add": cmd_add,
    "brief": cmd_brief,
    "cancel": cmd_cancel,
    "capabilities": cmd_capabilities,
    "config-show": cmd_config_show,
    "config-validate": cmd_config_validate,
    "doctor": cmd_doctor,
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
