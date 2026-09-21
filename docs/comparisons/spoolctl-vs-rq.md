---
title: spoolctl and RQ
description: Compare spoolctl, a daemonless SQLite job queue for shell commands, with RQ (Redis Queue), the Python task queue backed by Redis.
bucket: concepts
order: 260
---

# spoolctl and RQ

**Research date:** 2026-09-21
**spoolctl source baseline:** `main`, commit [`f95600b`](https://github.com/Ozhiaki/spoolctl/tree/f95600bc9b3a93737f93c2e811b55b0129bb3cb6)
**RQ source baseline:** v2.12, commit [`90a67a1`](https://github.com/rq/rq/tree/90a67a159ef9fa055c6bde12ee1bb3ebd0440b84)

## Conclusion

RQ and spoolctl both recover a job whose worker died. They differ in what a
job is, and in what they need to run.

Use [**RQ**](https://github.com/rq/rq) when the work is Python functions and
you already run Redis or Valkey. It has retries, dependencies, scheduling,
cron, callbacks, and result storage. It can spread workers over many hosts
that share one Redis.

Use **spoolctl** when the work is shell commands, or when you cannot run a
server. spoolctl needs one SQLite file and the Python standard library. It
also reclaims a job only after it confirms that the owner process is dead.
RQ reclaims a job when its heartbeat entry expires.

## Scope and architecture

An RQ worker takes a job from a Redis list and forks a child process, the
"horse," to run it. The job goes into the queue's `StartedJobRegistry` with an
expiry time. The worker renews that expiry with heartbeats.

If the worker dies, the heartbeats stop and the entry expires. A later
maintenance pass of any worker runs `StartedJobRegistry.cleanup`. For each
expired job still in the STARTED state, it calls failure callbacks with
`AbandonedJobError`. Then it retries the job if retries are left. Otherwise it
moves the job to the `FailedJobRegistry`. See the pinned
[cleanup code](https://github.com/rq/rq/blob/90a67a159ef9fa055c6bde12ee1bb3ebd0440b84/rq/registry.py#L266-L335).
The maintenance interval defaults to 10 minutes.

spoolctl follows the same idea with one more check. When a heartbeat goes
stale, a worker asks the OS whether the owner process is alive. It reclaims
the job only if the owner is gone. If the answer is unclear, it leaves the
job alone. See [Guarantees](/docs/guarantees/).

## Capability comparison

| Area | RQ v2.12 | spoolctl at `f95600b` |
| --- | --- | --- |
| Unit of work | A Python function in your codebase. | A shell command. |
| Storage | Redis 5+ or Valkey 7.2+. | One SQLite file. |
| Server process | Redis or Valkey. | None. |
| Running job after worker death | Moved out of `StartedJobRegistry` when its entry expires; retried or failed. | Reclaimed after the owner is confirmed dead; run again. |
| Reclaim test | Heartbeat expiry. | Stale heartbeat plus a process liveness check. |
| Retry | `Retry(max, interval)`; interval can be a list. | Exponential backoff, capped at 60 s, then `dead`. |
| Failed job record | `FailedJobRegistry` with traceback. | `failed` and `dead` states, attempt history, error codes. |
| Timeout | Yes. Default 180 s. | Yes. Default 300 s. Kills the process group. |
| Dependencies | Yes, `depends_on`. | No. |
| Scheduling | `enqueue_at`, `enqueue_in`, and cron. | `--after`, `--at`. No recurring schedule. |
| Multi-host workers | Yes, through shared Redis. | No. One machine. |
| Submit from a shell | Needs Python code. | `spoolctl add`. |
| Install | Python package plus a Redis server. | Python 3 standard library only. |
| Platforms | Fork-based workers on POSIX; `SpawnWorker` for Windows. | macOS, Linux. |

## What RQ does well

- It recovers abandoned jobs. That is rarer than it should be among task
  queues.
- Its registries make job state easy to inspect.
- Retry intervals can differ per attempt, for example `[10, 30, 60]`.
- It has dependencies, cron, callbacks, and multi-host workers. spoolctl has
  none of these.
- It kills the horse's process group, not only the horse.

The pinned [exceptions docs](https://github.com/rq/rq/blob/90a67a159ef9fa055c6bde12ee1bb3ebd0440b84/docs/docs/exceptions.md?plain=1#L151-L154) and [workers docs](https://github.com/rq/rq/blob/90a67a159ef9fa055c6bde12ee1bb3ebd0440b84/docs/docs/workers.md?plain=1) describe these features.

## What spoolctl does differently

RQ needs a Redis server. spoolctl needs a file. In a sandbox, a CI job, or an
agent environment, that difference decides the choice.

RQ treats an expired heartbeat as proof of death. A worker that is alive but
stalled can lose its job to another worker. spoolctl checks the process first.
Recovery can be slower, but two live workers never own the same attempt.

spoolctl runs any command. No application code must import it.

## Composition

An RQ job can call `spoolctl add` to hand a shell command to a local durable
queue. A spoolctl job can run a script that enqueues RQ jobs. Each tool then
owns its own retries.

## Limits and claims not to make

- Do not say RQ loses jobs when a worker dies. It recovers them through
  registry cleanup.
- Do not say RQ has no retry, timeout, or Windows story. It has all three.
- Do not say spoolctl scales across hosts. It is one machine.
- Do not call either tool exactly-once. Both can run a job again after a
  crash.

## Sources

- [RQ README](https://github.com/rq/rq/blob/90a67a159ef9fa055c6bde12ee1bb3ebd0440b84/README.md?plain=1)
- [RQ StartedJobRegistry cleanup](https://github.com/rq/rq/blob/90a67a159ef9fa055c6bde12ee1bb3ebd0440b84/rq/registry.py#L266-L335)
- [RQ Retry class](https://github.com/rq/rq/blob/90a67a159ef9fa055c6bde12ee1bb3ebd0440b84/rq/job.py#L1896-L1926)
- [RQ AbandonedJobError](https://github.com/rq/rq/blob/90a67a159ef9fa055c6bde12ee1bb3ebd0440b84/docs/docs/exceptions.md?plain=1#L151-L154)
- [RQ worker classes](https://github.com/rq/rq/blob/90a67a159ef9fa055c6bde12ee1bb3ebd0440b84/docs/docs/workers.md?plain=1)
- [spoolctl guarantees](https://github.com/Ozhiaki/spoolctl/blob/f95600bc9b3a93737f93c2e811b55b0129bb3cb6/docs/guarantees.md?plain=1)
