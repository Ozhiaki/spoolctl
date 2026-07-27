# Architecture

## SQLite is the daemon

spoolctl has no long-running server process. The SQLite database file is the coordinator:

```
 spoolctl add ──────────────► ┌───────────────────┐
                              │   queue.db         │
 spoolctl work ─┐  claim/     │   (SQLite, WAL)    │ ◄──── spoolctl status
 spoolctl work ─┼─ heartbeat/ │                    │ ◄──── spoolctl output
 spoolctl work ─┘  record     │  jobs · attempts   │ ◄──── spoolctl retry
      │                       └───────────────────┘
      ▼
  fork/exec job in its own process group
  enforce timeout · capture stdout/stderr
```

Every operation -- add, claim, status, cancel, prune -- is a transaction against the same database file. Workers are symmetric peers: they claim jobs, heartbeat, and record results through the same interface. No worker is special. If all workers die, the database holds the full queue state and the next worker picks up where they left off.

## WAL mode

The database is opened with `journal_mode=WAL` and `synchronous=NORMAL`. WAL mode allows concurrent readers alongside a single writer without blocking. All write operations use `BEGIN IMMEDIATE` to take the write lock at transaction open, avoiding the upgrade-race deadlock that can occur with deferred transactions.

`busy_timeout` is set to 5 seconds. A writer that cannot acquire the lock within this window receives a `LOCKED` error (exit code 4, retryable).

## Tables

### `jobs`

The primary queue table. Each row is one job: its argv, state, scheduling metadata, retry budgets, lock ownership, and timestamps. Key columns:

- `state` -- one of `queued`, `running`, `done`, `failed`, `dead`, `canceled`
- `priority`, `queue`, `next_run_at` -- claim ordering
- `locked_by`, `locked_pid`, `heartbeat_at` -- worker ownership tracking
- `max_retries`, `max_crashes`, `attempts`, `crashes` -- retry budgets
- `idempotency_key` -- optional deduplication key
- `tags_json`, `note` -- metadata for cross-session handoff
- `cwd`, `env_json` -- execution environment

The covering index `idx_jobs_claimable` supports the claim query: `(state, queue, priority DESC, next_run_at ASC, id ASC)`.

### `attempts`

One row per execution attempt. Records the worker, start/finish times, exit code, stdout/stderr file paths, and failure reason. A job with three retries has four attempt rows.

### `job_events`

Append-only ledger of state transitions. Each event records the job ID, event type, timestamp, and optional worker ID and detail. See [Events](events.md).

### `meta`

Single-row key-value table holding `schema_version`.

## Schema version history

| Version | Changes |
|---|---|
| v1 | Initial schema: `jobs`, `attempts`, `job_events`, `meta`. |
| v2 | Added `canceled` to the job and attempt state CHECK constraints. Table rebuild required because SQLite cannot ALTER a CHECK constraint. |
| v3 | Added `idempotency_key`, `tags_json`, `note` to `jobs`. Created partial index on `idempotency_key`. |
| v4 | Added `priority`, `queue` to `jobs`. Rebuilt `idx_jobs_claimable` as `(state, queue, priority DESC, next_run_at ASC, id ASC)`. |
| v5 | Added `cwd`, `env_json`, `crashes`, `max_crashes` to `jobs`. Backfilled `crashes` from existing `abandoned` attempts with `error='worker died'`. |
| v6 | Added `failure_reason` to `attempts`. Backfilled from existing attempt state and error: `timed_out` → `timeout`, `abandoned` + `worker died` → `worker_crash`, `canceled` → `canceled`, `failed` → `unknown`. The `unknown` backfill is conservative: historical `failed` rows have no reliable way to distinguish `process_exit` from `spawn_failed` without parsing human-readable error text, which the migration deliberately refuses to do. |

Migrations run automatically on database open, one version step at a time. Each step uses `BEGIN IMMEDIATE` and re-reads the version inside the transaction so a losing racer (two processes opening the same database simultaneously) no-ops instead of double-applying.

## When no worker is running

The database is inert. No background threads, no timers, no daemon. Jobs stay in their current state until a worker process opens the database and resumes. Heartbeats stop, but no reaping happens because there is no reaper running. The queue is a static file on disk.

This is the key architectural difference from tools that require a daemon: spoolctl can be stopped and restarted at any time without data loss or state corruption. The cost is that recovery from a crashed worker requires another worker to start and run its reap cycle.
