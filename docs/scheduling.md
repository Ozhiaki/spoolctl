---
title: Scheduling
description: Schedule spoolctl jobs with delays, priorities, queues, and drain mode.
bucket: guides
order: 30
---

# Scheduling

## Delayed submission

Jobs can be delayed at submit time:

- `--after <duration>` delays the job by a relative amount. Grammar: `<number>[s|m|h|d]` or bare seconds. `--after 5m` means "eligible 5 minutes from now."
- `--at <timestamp>` sets an absolute eligibility time. Accepts epoch seconds or ISO-8601. Timestamps in the past clamp to now.

`--after` and `--at` are mutually exclusive.

A delayed job enters state `queued` with a future `next_run_at`. It is not a separate state; `scheduled` is a derived count (see [Concepts](/docs/concepts/)).

## Priorities

`--priority <N>` sets an integer priority (default: 0). Workers claim the highest-priority eligible job first. Within the same priority, the job with the earliest `next_run_at` is claimed first; ties are broken by job ID.

The full claim order is: `priority DESC, next_run_at ASC, id ASC`.

Priority range: -2147483648 to 2147483647 (SQLite integer).

## Named queues (lanes)

`--queue <name>` assigns a job to a named lane (default: `default`). Queue names are 1-64 characters.

Workers target a specific queue: `work --queue gpu`. A worker only claims jobs from its configured queue.

### Slot ceiling

`work --queue gpu --slots 2` sets a per-lane ceiling: the worker will not claim a new job in the `gpu` queue if 2 or more jobs are already running there. The check counts all running jobs in the queue across all workers, so the ceiling is a fleet-wide concurrency limit, not per-worker.

Multiple workers can serve the same queue with the same `--slots` value. Each independently checks the running count before claiming, and the `BEGIN IMMEDIATE` transaction serializes the actual claim.

### GPU-lane example

```bash
python3 -m spoolctl add --queue gpu --priority 10 -- python train.py --model large
python3 -m spoolctl add --queue gpu --priority 5  -- python train.py --model small
python3 -m spoolctl add --queue cpu -- python preprocess.py

python3 -m spoolctl work --queue gpu --slots 2 &
python3 -m spoolctl work --queue cpu &
```

The GPU worker claims at most 2 concurrent GPU training jobs, highest priority first. The CPU worker handles preprocessing independently.

### Holding a lane (draining it)

`hold --queue gpu` holds a lane: from then on no worker claims a new job from it, while the jobs already running there finish normally; nothing is killed. Queued jobs wait, and other lanes are unaffected. The hold is stored in the database, so it survives restarts, until `hold --queue gpu --release` lets workers claim from the lane again. Both are idempotent: `data.changed` says whether the call changed anything, and `data.running` / `data.queued` how many jobs are still running and waiting in the lane. `status --json` lists held lanes in `data.holds`.

Use it to take a resource out of service gracefully (maintenance, a host going away): hold its lane, wait for `running` to reach 0, then work on it. A `work --drain` worker on a held lane exits once the lane's running jobs finish, since nothing will claim its queued jobs until release.

```bash
python3 -m spoolctl hold --queue gpu --json            # running jobs finish; none start
python3 -m spoolctl hold --queue gpu --release --json  # claim again
```

## Drain semantics

When a worker has no more claimable jobs, it checks whether the queue is "settled" before stopping:

- Running jobs hold drain open.
- Queued jobs that are currently eligible hold drain open.
- Queued retry/reap backoff rows (attempts > 0, future `next_run_at`) hold drain open.
- Queued user-delayed rows (attempts = 0, future `next_run_at`) do **not** hold drain open.
- Queued rows of a held lane (`hold --queue`) do **not** hold drain open: nothing claims them until the lane is released.

The distinction matters: a worker that has finished all current work will stop even if user-delayed jobs are scheduled for later. Those jobs will be served when a worker is running at their eligible time.

## User delay vs retry backoff

Both `--after 60` (user delay) and an automatic retry backoff produce a queued job with a future `next_run_at`. The difference:

- User delay: `attempts = 0`, the job has never run. Does not hold drain open.
- Retry backoff: `attempts > 0`, the job has run and failed. Holds drain open because the worker's job is not finished until retries are exhausted.

`status` reports both as part of the `scheduled` count. The distinction is visible in the `attempts` field.
