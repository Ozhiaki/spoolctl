---
title: spoolctl and Celery
description: Compare spoolctl with Celery, the distributed task queue. Celery runs application functions through a broker; spoolctl runs shell commands from one SQLite file and re-runs a job after a confirmed-dead worker.
bucket: concepts
order: 300
---

# spoolctl and Celery

**Research date:** 2026-09-21
**spoolctl source baseline:** `main`, commit [`f95600b`](https://github.com/Ozhiaki/spoolctl/tree/f95600bc9b3a93737f93c2e811b55b0129bb3cb6)
**Celery source baseline:** `main`, commit [`1ba6258`](https://github.com/celery/celery/tree/1ba6258e1679b2bbd7561f659d3ead46caf72045)

## Conclusion

Celery is the best-known distributed task queue for Python. It runs application
functions on a pool of worker processes, coordinated through a message broker
(RabbitMQ or Redis). It has retries with exponential backoff, time limits,
routing, scheduling, and a large ecosystem. It scales across many machines.

It is a different kind of tool from spoolctl, in the same class as
[huey](/docs/comparisons/spoolctl-vs-huey/) and
[RQ](/docs/comparisons/spoolctl-vs-rq/). The unit of work is a Python function in
your application, not a shell command. It needs a broker process to run at all.
And its crash behavior is the subtle part: by default a task is acknowledged just
before it runs, so a task killed mid-run is not run again.

Use **Celery** when you have a Python application, already run a broker, and want
distributed task processing with a deep feature set. Use **spoolctl** when you
want to queue shell commands on one machine, with no broker and no application,
and have a job re-run automatically after the worker that held it is confirmed
dead.

## Scope and architecture

Celery communicates through messages. A client sends a task message to a broker;
the broker delivers it to a worker. Celery requires a message transport to send
and receive messages; RabbitMQ and Redis are the feature-complete brokers. A
separate result backend stores task results if you want them.

The worker is usually a prefork pool: a parent process and child processes that
execute tasks. Concurrency comes from the pool size and from running more workers,
on one host or many, all fed by the shared broker.

spoolctl has no broker, no result backend, and no application. Jobs are rows in
one SQLite file. Any number of peer workers claim jobs, each in one transaction,
and write a heartbeat while a job runs. A job is reclaimed only after its owner
process is confirmed dead. See [Guarantees](/docs/guarantees/).

## The crash question, precisely

This is the claim that is easy to get wrong, so state it carefully.

A task message stays on the queue until a worker acknowledges it. If a worker
reserves a message and then dies **before** starting it, the message is
redelivered to another worker. That is queue-level durability, and Celery has it.

But the default is to acknowledge the message **in advance, just before it is
executed**, so that a task which already started is never run twice. The
consequence: if the worker is SIGKILLed **while the task is running**, the message
is already acknowledged, and the task is not redelivered. It is lost.

`task_acks_late` moves the acknowledgement to after the task returns. Even then,
Celery documents that when the child process running the task is terminated by a
signal (for example SIGKILL), the worker still acknowledges the message, on
purpose, so a segfaulting or OOM-killed task does not loop. To make such a task
redeliver you must also enable `task_reject_on_worker_lost`, which the docs warn
"can cause message loops."

So Celery can be configured to re-run a task after a hard kill, but it is off by
default and comes with a loop warning. spoolctl re-runs by default after a
confirmed-dead owner, and its crash budget (`max-crashes`, then `dead`) is what
stops the loop.

## Capability comparison

| Area | Celery at `1ba6258` | spoolctl at `f95600b` |
| --- | --- | --- |
| Unit of work | A Python function (`@app.task`) in your application. | Any shell command. |
| Coordinator | A message broker (RabbitMQ or Redis), plus worker processes. | None. One SQLite file. |
| Queue state survives coordinator death | Depends on the broker's durability. | Yes. Durable in SQLite. |
| Running job survives runner SIGKILL | No by default (acked before execution). Configurable: `acks_late` **and** `task_reject_on_worker_lost`, with a loop warning. | Yes. Re-run after the owner is confirmed dead. |
| Never run twice at once | Default early ack prevents a started task running twice. | Enforced. One claim per attempt in one transaction. |
| Automatic retry | Yes: `autoretry_for`, or `self.retry()`. | Built in. |
| Retry backoff | Optional exponential: `retry_backoff=True`, with jitter, capped at 10 min. | Exponential by default. |
| Retry budget / failed state | `max_retries` (default 3), then the task fails. | `max-retries`, then `dead`. |
| Per-job timeout | Optional: `task_time_limit` (hard) and `task_soft_time_limit`. Default: no limit. | Yes. Default 300 s. Kills the process group. |
| Concurrency | Prefork pool plus more workers, across machines. | Lanes, each with an optional fleet-wide `--slots` ceiling. |
| Output | Return value to the result backend; logs to the worker log. | Files per attempt; `output` verb from any process. |
| Machine interface | Python API; events; `inspect`/`control`; Flower. | JSON envelope on every verb, stable error codes, schemas. |
| Install | `pip install celery`, plus a broker service (RabbitMQ or Redis), and your app. | Python 3 standard library only. No broker. No root. |
| Platforms | Python, wherever a broker and the prefork pool run. | macOS, Linux. |

## What Celery does well

- It is a mature distributed task queue. Work spreads across many workers and
  many machines through the broker.
- It retries with optional exponential backoff and jitter, and enforces hard and
  soft time limits per task.
- It has routing, rate limits, scheduled tasks (Celery beat), chords and chains,
  and a large ecosystem (Flower, framework integrations).
- With `task_acks_late` and `task_reject_on_worker_lost` it can redeliver a task
  after a worker is lost, if you accept the loop risk.

## What spoolctl does differently

- spoolctl runs shell commands, not application functions. There is no code to
  import and no app to deploy.
- spoolctl needs no broker and no result backend. One SQLite file is the whole
  coordination layer.
- spoolctl re-runs a job after a confirmed-dead owner by default, from peer
  workers with no central broker, and bounds the retries with a crash budget so a
  poison job stops in `dead` instead of looping.
- spoolctl has no distributed, multi-host model. It is a one-machine tool, and
  says so.

## Composition

They sit at different layers and rarely combine. If you already run Celery, use
it. spoolctl is for the machine, script, or sandbox that should not stand up a
broker and an application just to queue a few shell commands crash-safely. A
Celery task could shell out to `spoolctl add`, but that is unusual; the normal
choice is one tool or the other.

## Limits and claims not to make

- Do not say Celery loses every job on a worker crash. A message reserved but not
  yet started is redelivered. The loss case is a task killed **while running**
  under the default early ack.
- Do not say Celery cannot re-run a killed task. `task_acks_late` with
  `task_reject_on_worker_lost` can, with a documented loop risk.
- Do not say Celery has no backoff or no timeout. `retry_backoff=True` gives
  exponential backoff; `task_time_limit`/`task_soft_time_limit` bound runtime.
- Do not call Celery broker-optional. It requires a message transport.
- Do not compare units directly. Celery runs Python functions; spoolctl runs
  shell commands.

## Sources

- [Celery introduction](https://github.com/celery/celery/blob/1ba6258e1679b2bbd7561f659d3ead46caf72045/docs/getting-started/introduction.rst?plain=1#L63-L64) — Celery requires a message transport (RabbitMQ or Redis).
- [Task acknowledgement and redelivery](https://github.com/celery/celery/blob/1ba6258e1679b2bbd7561f659d3ead46caf72045/docs/userguide/tasks.rst?plain=1#L15-L47) — a message is redelivered if the worker dies before starting it; the default acks in advance just before execution; even with `acks_late`, a signal-killed child is still acked unless `task_reject_on_worker_lost` is set.
- [`task_acks_late`](https://github.com/celery/celery/blob/1ba6258e1679b2bbd7561f659d3ead46caf72045/docs/userguide/configuration.rst?plain=1#L651-L657) — default disabled. [`task_reject_on_worker_lost`](https://github.com/celery/celery/blob/1ba6258e1679b2bbd7561f659d3ead46caf72045/docs/userguide/configuration.rst?plain=1#L714-L732) — default disabled; "can cause message loops." [`task_time_limit`](https://github.com/celery/celery/blob/1ba6258e1679b2bbd7561f659d3ead46caf72045/docs/userguide/configuration.rst?plain=1#L566-L571) — default no time limit.
- [`retry_backoff`](https://github.com/celery/celery/blob/1ba6258e1679b2bbd7561f659d3ead46caf72045/docs/userguide/tasks.rst?plain=1#L736-L751) — optional exponential backoff with jitter, capped at 10 minutes.
- [`Task.max_retries`/`default_retry_delay`](https://github.com/celery/celery/blob/1ba6258e1679b2bbd7561f659d3ead46caf72045/celery/app/task.py#L241-L245) — default 3 retries, 180 s delay.
- [spoolctl guarantees](https://github.com/Ozhiaki/spoolctl/blob/f95600bc9b3a93737f93c2e811b55b0129bb3cb6/docs/guarantees.md?plain=1)
