"""Parser-independent input parsing and validation helpers."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from datetime import datetime, tzinfo

from spoolctl.errors import CliError
from spoolctl.models import (
    DECIMAL_RE,
    INT_RE,
    JOB_STATES,
    MAX_DURATION_SECONDS,
    MAX_ENV_KEY_CHARS,
    MAX_ENV_VALUE_CHARS,
    MAX_PATH_CHARS,
    MAX_WAIT_SECONDS,
    MAX_WORKER_ID_CHARS,
    PRIORITY_MAX,
    PRIORITY_MIN,
    PRUNABLE_STATES,
    QUEUE_RE,
    SIGNED_DECIMAL_RE,
    SQLITE_INT64_MAX,
    SQLITE_INT64_MIN,
    TAG_KEY_RE,
    _suggest,
)


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


def _parse_int_bound(
    raw: str | int,
    *,
    flag: str,
    minimum: int = SQLITE_INT64_MIN,
    maximum: int = SQLITE_INT64_MAX,
) -> int:
    if isinstance(raw, int):
        value = raw
    else:
        if not INT_RE.fullmatch(raw):
            raise CliError(
                "INVALID_INPUT",
                f"{flag} must be an integer (got {raw!r})",
                f"try: {flag} {max(minimum, 0)}",
            )
        value = int(raw, 10)
    if value < minimum or value > maximum:
        raise CliError(
            "INVALID_INPUT",
            f"{flag} must be in [{minimum}, {maximum}] (got {value})",
            f"try: {flag} {max(minimum, 0)}",
        )
    return value


def _parse_positive_float(
    raw: str | float,
    *,
    flag: str,
    maximum: float = MAX_WAIT_SECONDS,
) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise CliError(
            "INVALID_INPUT",
            f"{flag} must be a finite number (got {raw!r})",
            f"try: {flag} 0.5",
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise CliError(
            "INVALID_INPUT",
            f"{flag} must be finite and > 0 (got {raw!r})",
            f"try: {flag} 0.5",
        )
    if value > maximum:
        raise CliError(
            "INVALID_INPUT",
            f"{flag} must be <= {maximum:g} (got {raw!r})",
            f"try: {flag} 0.5",
        )
    return value


def _parse_tag_key(raw: str, *, flag: str) -> str:
    if not raw or not TAG_KEY_RE.fullmatch(raw):
        raise CliError(
            "INVALID_INPUT",
            f"bad {flag} key: {raw!r}",
            "tag keys must match [A-Za-z0-9_.:-]+",
        )
    if len(raw) > 128:
        raise CliError(
            "INVALID_INPUT",
            f"{flag} key must be <= 128 characters (got {len(raw)})",
            "use a shorter tag key",
        )
    return raw


def _parse_add_tags(raw_tags: list[str]) -> dict[str, str]:
    tags: dict[str, str] = {}
    if len(raw_tags) > 16:
        raise CliError(
            "INVALID_INPUT",
            f"at most 16 tags are allowed (got {len(raw_tags)})",
            "remove extra --tag flags",
        )
    for raw in raw_tags:
        if "=" not in raw:
            raise CliError(
                "INVALID_INPUT",
                f"bad --tag {raw!r}; expected KEY=VALUE",
                "try: spoolctl add --tag owner=agent -- <cmd>",
            )
        key, value = raw.split("=", 1)
        key = _parse_tag_key(key, flag="--tag")
        if key in tags:
            raise CliError(
                "INVALID_INPUT",
                f"duplicate --tag key: {key!r}",
                "provide each tag key at most once",
            )
        if len(value) > 1024:
            raise CliError(
                "INVALID_INPUT",
                f"--tag {key!r} value must be <= 1024 characters (got {len(value)})",
                "use a shorter tag value",
            )
        tags[key] = value
    return tags


def _parse_job_env(raw_env: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in raw_env:
        if "=" not in raw:
            raise CliError(
                "INVALID_INPUT",
                f"bad --env {raw!r}; expected K=V",
                "try: spoolctl add --env FOO=bar -- <cmd>",
                did_you_mean="K=V",
            )
        key, value = raw.split("=", 1)
        if not key:
            raise CliError(
                "INVALID_INPUT",
                "bad --env key: key must not be empty",
                "try: spoolctl add --env FOO=bar -- <cmd>",
                did_you_mean="K=V",
            )
        if len(key) > MAX_ENV_KEY_CHARS:
            raise CliError(
                "INVALID_INPUT",
                f"--env key must be <= {MAX_ENV_KEY_CHARS} characters (got {len(key)})",
                "use a shorter environment variable name",
                did_you_mean="K=V",
            )
        if len(value) > MAX_ENV_VALUE_CHARS:
            raise CliError(
                "INVALID_INPUT",
                f"--env {key!r} value must be <= {MAX_ENV_VALUE_CHARS} characters (got {len(value)})",
                "use a shorter environment value",
                did_you_mean="K=V",
            )
        if "\x00" in key or "\x00" in value:
            raise CliError(
                "INVALID_INPUT",
                "bad --env value: NUL bytes are not allowed",
                "remove embedded NUL bytes from --env K=V",
                did_you_mean="K=V",
            )
        env[key] = value
    return env


def _parse_job_cwd(raw: str | None, *, base: str | None = None) -> str | None:
    if raw is None:
        return None
    if raw == "":
        raise CliError(
            "INVALID_INPUT",
            "--cwd must not be empty",
            "try: spoolctl add --cwd . -- <cmd>",
        )
    if "\x00" in raw:
        raise CliError(
            "INVALID_INPUT",
            "--cwd must not contain NUL bytes",
            "remove embedded NUL bytes from --cwd",
        )
    if len(raw) > MAX_PATH_CHARS:
        raise CliError(
            "INVALID_INPUT",
            f"--cwd must be <= {MAX_PATH_CHARS} characters (got {len(raw)})",
            "use a shorter working-directory path",
        )
    root = os.getcwd() if base is None else base
    path = raw if os.path.isabs(raw) else os.path.join(root, raw)
    return os.path.abspath(path)


def _parse_worker_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    if not raw:
        raise CliError(
            "INVALID_INPUT",
            "--worker-id must not be empty",
            "try: spoolctl work --worker-id worker-1",
        )
    if len(raw) > MAX_WORKER_ID_CHARS:
        raise CliError(
            "INVALID_INPUT",
            f"--worker-id must be <= {MAX_WORKER_ID_CHARS} characters (got {len(raw)})",
            "use a shorter worker id",
        )
    if not raw.isprintable():
        raise CliError(
            "INVALID_INPUT",
            "--worker-id must contain only printable characters",
            "remove embedded newlines, tabs, or control characters",
        )
    return raw


def _parse_list_tags(raw_tags: list[str]) -> list[tuple[str, str | None]]:
    predicates = []
    for raw in raw_tags:
        if "=" in raw:
            key, value = raw.split("=", 1)
        else:
            key, value = raw, None
        predicates.append((_parse_tag_key(key, flag="--tag"), value))
    return predicates


def _finite_decimal(raw: str, *, signed: bool, flag: str) -> float:
    pattern = SIGNED_DECIMAL_RE if signed else DECIMAL_RE
    if not pattern.fullmatch(raw):
        raise CliError(
            "INVALID_INPUT",
            f"{flag} expects a finite decimal number (got {raw!r})",
            f"try: {flag} 30s   or: {flag} 1700000000",
        )
    value = float(raw)
    if not math.isfinite(value):
        raise CliError(
            "INVALID_INPUT",
            f"{flag} must be finite (got {raw!r})",
            f"try: {flag} 30s   or: {flag} 1700000000",
        )
    return value


def _parse_duration_seconds(raw: str, *, flag: str) -> float:
    unit = raw[-1:] if raw else ""
    multipliers = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    if unit.isalpha():
        if unit not in multipliers:
            raise CliError(
                "INVALID_INPUT",
                f"bad {flag} duration: {raw!r}",
                "duration grammar is <number>[s|m|h|d], lowercase units only, or bare seconds",
            )
        number = raw[:-1]
        multiplier = multipliers[unit]
    else:
        number = raw
        multiplier = 1.0
    seconds = _finite_decimal(number, signed=False, flag=flag) * multiplier
    if not math.isfinite(seconds) or seconds > MAX_DURATION_SECONDS:
        raise CliError(
            "INVALID_INPUT",
            f"{flag} duration must be <= {MAX_DURATION_SECONDS:g} seconds",
            "use a shorter duration",
        )
    return seconds


def _parse_after(raw: str) -> float:
    return _parse_duration_seconds(raw, flag="--after")


def _parse_at(raw: str, *, tz: tzinfo | None = None) -> float:
    if SIGNED_DECIMAL_RE.fullmatch(raw):
        value = _finite_decimal(raw, signed=True, flag="--at")
        if abs(value) > MAX_DURATION_SECONDS:
            raise CliError(
                "INVALID_INPUT",
                f"--at epoch seconds must be in [-{MAX_DURATION_SECONDS:g}, {MAX_DURATION_SECONDS:g}]",
                "use a nearer epoch seconds value or an ISO-8601 timestamp",
            )
        return value
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise CliError(
            "INVALID_INPUT",
            f"bad --at timestamp: {raw!r}",
            "use epoch seconds or ISO-8601, e.g. 2026-07-16T09:00:00-04:00",
        ) from None
    if tz is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    value = dt.timestamp()
    if abs(value) > MAX_DURATION_SECONDS:
        raise CliError(
            "INVALID_INPUT",
            f"--at timestamp must be within {MAX_DURATION_SECONDS:g} seconds of epoch",
            "use a nearer timestamp",
        )
    return value


def _parse_priority(raw: str, *, flag: str = "--priority") -> int:
    return _parse_int_bound(raw, flag=flag, minimum=PRIORITY_MIN, maximum=PRIORITY_MAX)


def _parse_queue(raw: str, *, flag: str = "--queue") -> str:
    if raw.strip() != raw:
        raise CliError(
            "INVALID_INPUT",
            f"{flag} must not have leading or trailing whitespace",
            "queue names must match ^[A-Za-z0-9][A-Za-z0-9._-]*$",
        )
    if not (1 <= len(raw) <= 64) or not QUEUE_RE.fullmatch(raw):
        raise CliError(
            "INVALID_INPUT",
            f"bad {flag} name: {raw!r}",
            "queue names must be 1-64 chars and match ^[A-Za-z0-9][A-Za-z0-9._-]*$",
        )
    return raw


def _validate_positive_float_env(
    name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    values = os.environ if env is None else env
    raw = values.get(name)
    if raw is None:
        return
    _parse_positive_float(raw, flag=name, maximum=MAX_WAIT_SECONDS)


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


def _job_id_arg(raw: str) -> int:
    return _parse_int_bound(raw, flag="job id", minimum=1)


def _parse_duration(raw: str) -> float:
    """DURATION grammar: bounded decimal with optional s|m|h|d suffix."""
    return _parse_duration_seconds(raw, flag="--older-than")


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
