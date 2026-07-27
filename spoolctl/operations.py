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
