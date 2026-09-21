---
title: spoolctl and huey
description: Compare spoolctl, a crash-safe shell job queue, with huey, the lightweight Python task queue with a SQLite backend.
bucket: concepts
order: 250
---

# spoolctl and huey

**Research date:** 2026-09-21
**spoolctl source baseline:** `main`, commit [`f95600b`](https://github.com/Ozhiaki/spoolctl/tree/f95600bc9b3a93737f93c2e811b55b0129bb3cb6)
**huey source baseline:** 3.4.0, commit [`ffa956f`](https://github.com/coleifer/huey/tree/ffa956f5cfd89ddd11a0cf9255e72178dd10d19f)

## Conclusion

huey and spoolctl can both keep a queue in one SQLite file. They solve
different problems.

Use [**huey**](https://github.com/coleifer/huey) when the work is Python
functions inside an application. It has retries with backoff, priorities,
delays, periodic tasks, pipelines, locks, and result storage. spoolctl has no
periodic tasks, pipelines, or result store.

Use **spoolctl** when the work is shell commands, or when a job must run again
after its runner is killed. huey removes a task from storage when a consumer
takes it. Its documentation says a task interrupted by SIGKILL is lost.
spoolctl keeps the job row until the job ends, and reclaims it from a dead
worker.

## Scope and architecture

huey has three parts: the application that enqueues tasks, a storage backend,
and a consumer process that runs tasks with thread, process, or greenlet
workers. Backends include Redis, SQLite, files, and memory.

In the SQLite backend, `dequeue` selects the next task and deletes its row in
one transaction. See the pinned
[SqliteStorage.dequeue](https://github.com/coleifer/huey/blob/ffa956f5cfd89ddd11a0cf9255e72178dd10d19f/huey/storage.py#L952-L966).
From that moment the task exists only in the consumer's memory. The consumer
docs state: "Huey does not guarantee at-least-once delivery of messages."
A graceful shutdown lets running tasks finish. A SIGKILL or power loss does
not. huey emits `SIGNAL_INTERRUPTED` on an interrupted shutdown, and its docs
give a recipe to re-enqueue from that signal.

spoolctl keeps the job row and marks it claimed. A worker writes a heartbeat
while the job runs. If the heartbeat goes stale, another worker checks that
the owner process is gone, then runs the job again. See
[Guarantees](/docs/guarantees/).

## Capability comparison

| Area | huey 3.4.0 | spoolctl at `f95600b` |
| --- | --- | --- |
| Unit of work | A Python function in your codebase. | A shell command. |
| Storage | Redis, SQLite, file, or memory. | SQLite only. |
| Running task after runner SIGKILL | Lost. Row was deleted at dequeue. | Reclaimed and run again after the owner is confirmed dead. |
| Delivery | Not at-least-once, per its docs. | At-least-once. |
| Retry | `retries`, `retry_delay`, `retry_backoff` multiplier. | Exponential backoff, capped at 60 s, then `dead`. |
| Failed task record | Exception stored as the task result, if a result store is on. | `failed` and `dead` states, attempt history, error codes. |
| Timeout | Hard with process and greenlet workers; cooperative with threads. | Hard. Default 300 s. Kills the process group. |
| Priorities | Yes. | Yes. |
| Delayed start | Yes, `eta` or delay. | Yes: `--after`, `--at`. |
| Periodic tasks | Yes, crontab style. | No. |
| Pipelines, locks | Yes. | No. |
| Submit from a shell | Needs Python code. | `spoolctl add`. |
| Install | Python package. | Python 3 standard library only. |
| Platforms | Python; no process workers on Windows. | macOS, Linux. |

## What huey does well

- It is small for a full task queue, and its SQLite backend needs no server.
- Retries, backoff, priorities, delays, and periodic tasks are all built in.
- Its docs are honest about delivery. They name the SIGKILL case and give a
  recovery recipe.
- The docs recommend idempotent tasks. That advice applies to spoolctl too.

The pinned [task API docs](https://github.com/coleifer/huey/blob/ffa956f5cfd89ddd11a0cf9255e72178dd10d19f/docs/api.rst) document these options.

## What spoolctl does differently

huey trusts its consumer. spoolctl assumes the runner will die at the worst
time. That is the difference between delete-on-dequeue and a claimed row with
a heartbeat.

spoolctl runs any command. A job can be a Python script, a shell pipeline, or
a compiled binary. No application code must import spoolctl.

spoolctl kills the whole process group on timeout. A task that spawned
children does not leave them running.

## Composition

A huey task can call `spoolctl add` to hand a shell command to a durable
queue. A spoolctl job can run a script that enqueues huey tasks. Each tool
then owns its own retries.

## Limits and claims not to make

- Do not say huey has no retry or no backoff. It has both.
- Do not say huey has no timeout. It has one; with threads it is cooperative.
- Do not say every huey setup loses tasks on any crash. Queued tasks stay in
  storage. The loss is for a task that was running.
- Do not claim spoolctl has periodic tasks. Use cron or systemd timers to call
  `spoolctl add`.

## Sources

- [huey README](https://github.com/coleifer/huey/blob/ffa956f5cfd89ddd11a0cf9255e72178dd10d19f/README.rst)
- [huey SqliteStorage.dequeue](https://github.com/coleifer/huey/blob/ffa956f5cfd89ddd11a0cf9255e72178dd10d19f/huey/storage.py#L952-L966)
- [huey consumer docs, delivery guarantee](https://github.com/coleifer/huey/blob/ffa956f5cfd89ddd11a0cf9255e72178dd10d19f/docs/consumer.rst?plain=1#L288-L293)
- [huey deployment docs, SIGKILL](https://github.com/coleifer/huey/blob/ffa956f5cfd89ddd11a0cf9255e72178dd10d19f/docs/deployment.rst?plain=1#L46-L51)
- [huey task API](https://github.com/coleifer/huey/blob/ffa956f5cfd89ddd11a0cf9255e72178dd10d19f/docs/api.rst?plain=1#L357-L392)
- [spoolctl guarantees](https://github.com/Ozhiaki/spoolctl/blob/f95600bc9b3a93737f93c2e811b55b0129bb3cb6/docs/guarantees.md?plain=1)
