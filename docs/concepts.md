# Concepts

## Jobs

A job is a command to execute. It is created by `add` and lives in the `jobs` table as a row with an argv vector, scheduling metadata, retry budgets, and state. Each job has a unique integer ID assigned at creation.

## Attempts

Each time a worker executes a job, it creates an attempt row. The attempt records the worker ID, PID, start and finish times, exit code, and paths to captured stdout/stderr files. A job may have multiple attempts if it fails and is retried, or if the worker crashes and the job is re-queued.

## Job States

Jobs move through these states:

| State | Meaning |
|---|---|
| `queued` | Waiting to be claimed by a worker. |
| `running` | Currently being executed by a worker. |
| `done` | Completed successfully (exit code 0). |
| `failed` | Reserved and never emitted. A failing job with retry budget left returns to `queued`; it becomes `dead` once the budget is exhausted. |
| `dead` | Exhausted all retries; will not be retried automatically. |
| `canceled` | Explicitly canceled by the user. |

State transitions are always mediated by the database under `BEGIN IMMEDIATE` transactions, never by in-memory logic alone.

### Scheduled (derived, not a state)

A queued job with `next_run_at` in the future is reported as "scheduled" in status counts. This is a query-time derivation, not a stored state. A job delayed by `--after 60` and a job delayed by retry backoff both appear as scheduled; only the first reflects user intent.

## Attempt States

| State | Meaning |
|---|---|
| `running` | Currently executing. |
| `succeeded` | Completed with exit code 0. |
| `failed` | Completed with non-zero exit code. |
| `timed_out` | Exceeded the per-job timeout. |
| `abandoned` | Abandoned due to worker crash. |
| `canceled` | Canceled. |

## Event Ledger

Every state transition writes to the `job_events` table: an append-only ledger of `(job_id, event_type, timestamp, worker_id, detail)` rows. The ledger is the daemonless event stream; see [Events](events.md) for how to consume it.

Event types: `added`, `claimed`, `succeeded`, `failed`, `timed_out`, `reaped`, `dead`, `retried`, `canceled`.

## Queues (Lanes)

Jobs are assigned to a named queue (default: `default`). Workers can target a specific queue with `work --queue <name>`. The `--slots N` flag on `work` sets a per-lane ceiling: the worker will not claim a new job in that queue if N jobs are already running there. This is the fleet-wide concurrency control mechanism.

## Priorities

Jobs have an integer priority (default: 0, range: -2147483648 to 2147483647). Higher values are claimed first. Within the same priority, the job with the earliest eligible `next_run_at` is claimed first. Ties on both are broken by job ID (insertion order).

## Claim Order

Workers claim the next eligible job using:

```
priority DESC, next_run_at ASC, id ASC
```

This order is enforced by a covering index. Two workers racing to claim the same job are serialized by `BEGIN IMMEDIATE`; exactly one succeeds.

## Failure Reasons

When an attempt ends in failure, the reason is recorded:

| Reason | Recorded by |
|---|---|
| `process_exit` | Worker execution (non-zero exit code). |
| `timeout` | Worker execution (exceeded timeout). |
| `spawn_failed` | Worker execution (could not start the command). |
| `worker_crash` | Reaping (worker died without recording a result). |
| `canceled` | Cancel or force-retry. |
| `unknown` | Historical backfill for pre-v6 failures. |

See [Guarantees](guarantees.md) for how these interact with retry budgets.
