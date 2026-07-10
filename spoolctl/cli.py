"""argparse dispatch, envelope construction, human rendering, exit codes.

Thin over store/worker. Every verb speaks one machine contract: the
seven-key JSON envelope, the published exit-code dictionary, and errors
that teach the corrected invocation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
    CONTRACT_VERSION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIMEOUT_SECONDS,
    EXIT_CONFLICT,
    EXIT_ENVIRONMENT,
    EXIT_INPUT,
    EXIT_OK,
    EXIT_SAFETY,
    EXIT_TRANSIENT,
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
    """What a verb handler returns; the framework wraps it."""

    data: Any
    human: str
    warnings: list[dict[str, str]] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    stdout_silent: bool = False  # loop-mode work: nothing on stdout


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
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "ok": not errors,
        "tool_version": TOOL_VERSION,
        "data": data,
        "meta": {
            "request_id": "req_" + uuid.uuid4().hex[:12],
            "ts_iso": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "contract_version": CONTRACT_VERSION,
            "data_hash": canonical_data_hash(data),
        },
        "warnings": warnings or [],
        "commands": commands or [],
        "errors": errors or [],
    }


# --- parser -------------------------------------------------------------

VERBS = ("add", "work", "status", "retry", "output", "capabilities")

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
    add.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, metavar="SECONDS")
    add.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, metavar="N")
    add.add_argument("argv", nargs=argparse.REMAINDER, metavar="[--] ARGV...")

    work = sub.add_parser("work", parents=[common], help="run jobs until stopped")
    work.add_argument("--once", action="store_true", help="run at most one job, then exit")
    work.add_argument("--poll-interval", type=float, default=None, metavar="SECONDS")
    work.add_argument("--worker-id", default=None, metavar="NAME")

    status = sub.add_parser("status", parents=[common], help="queue counts and recent dead jobs")
    status.add_argument("--limit", type=int, default=10, metavar="N")

    retry = sub.add_parser("retry", parents=[common], help="requeue a dead or failed job")
    retry.add_argument("id", metavar="ID")
    retry.add_argument("--force", action="store_true", help="also requeue a running job (unsafe)")

    output = sub.add_parser("output", parents=[common], help="show a job's captured output")
    output.add_argument("id", metavar="ID")
    output.add_argument("--stream", choices=["stdout", "stderr", "both"], default="both")
    output.add_argument("--raw", action="store_true", help="raw bytes, single stream, no headers")
    output.add_argument("--attempt", type=int, default=None, metavar="N")

    caps = sub.add_parser("capabilities", parents=[common], help="machine-readable contract")

    _SUBPARSERS.clear()
    _SUBPARSERS.update(
        {"add": add, "work": work, "status": status, "retry": retry,
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

    conn = _open_db(args)
    try:
        job_id = store.add_job(conn, job_argv, args.timeout, args.max_retries, time.time())
    finally:
        conn.close()
    return VerbResult(
        data={"job_id": job_id, "state": "queued"},
        human=f"Added job {job_id}",
    )


def cmd_work(args: argparse.Namespace) -> VerbResult:
    if args.poll_interval is not None and args.poll_interval <= 0:
        raise CliError(
            "INVALID_INPUT",
            f"--poll-interval must be > 0 (got {args.poll_interval})",
            "try: spoolctl work --poll-interval 1.0",
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
    worker.work_loop(db_path, worker_id, poll)
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


def _job_id_arg(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise CliError(
            "INVALID_INPUT",
            f"job id must be an integer (got {raw!r})",
            "try: spoolctl status  (to list job ids)",
        ) from None


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


HANDLERS: dict[str, Callable[[argparse.Namespace], VerbResult]] = {
    "add": cmd_add,
    "retry": cmd_retry,
    "status": cmd_status,
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
            return EXIT_OK
        if json_mode:
            env = make_envelope(
                result.data,
                started=started,
                warnings=result.warnings,
                commands=result.commands,
            )
            print(json.dumps(env, ensure_ascii=False))
        else:
            if result.human:
                print(result.human)
            for w in result.warnings:
                print(f"warning: {w.get('message', w.get('code', ''))}", file=sys.stderr)
        return EXIT_OK
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
