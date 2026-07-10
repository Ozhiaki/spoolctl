"""argparse dispatch, envelope construction, human rendering, exit codes."""

from __future__ import annotations

import argparse
import sys

from spoolctl.models import EXIT_INPUT, EXIT_OK, TOOL_VERSION

HELP_EPILOG = """\
AGENT/AUTOMATION:
  Run `spoolctl capabilities --json` for the full machine-readable contract:
  verbs, flags, data schemas, exit codes, error codes.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spoolctl",
        description="Local job queue with retries, backoff, and crash recovery.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"spoolctl {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="verb", metavar="VERB")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", metavar="PATH", help="queue database path")
    common.add_argument("--json", action="store_true", help="emit the JSON envelope")

    add = sub.add_parser("add", parents=[common], help="enqueue a command")
    add.add_argument("-c", dest="shell_string", metavar="STRING", help="run STRING via sh -c")
    add.add_argument("--timeout", type=float, default=None, metavar="SECONDS")
    add.add_argument("--max-retries", type=int, default=None, metavar="N")
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

    sub.add_parser("capabilities", parents=[common], help="machine-readable contract")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verb is None:
        parser.print_help()
        return EXIT_OK
    print(f"spoolctl {args.verb}: not implemented yet", file=sys.stderr)
    return EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
