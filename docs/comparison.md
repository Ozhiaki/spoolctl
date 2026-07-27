# Comparison

## Summary table

|  | spoolctl | pueue | task-spooler | nq | queue libraries |
|---|---|---|---|---|---|
| Jobs are shell commands | yes | yes | yes | yes | no (jobs are code) |
| No daemon required | yes | no (`pueued`) | no | yes | n/a (embedded) |
| Concurrent independent workers | yes | within one daemon | no | no | yes |
| Job survives SIGKILL of its runner | yes | no | no | no | yes |
| Automatic retry + backoff | yes | no | no | no | yes |
| Dead-letter state | yes | no | no | no | varies |
| Per-job timeout, process-group kill | yes | no | no | no | varies |
| Delays, priorities, named resource lanes | yes | partial | partial | no | varies |
| Runtime dependencies | none | Rust binary + daemon | C binary + daemon | C binary | app + pip/broker |

## pueue

pueue is a great tool for a human supervising long-running commands. It has a richer interactive surface: pause/resume, task dependencies, a TUI-grade status display, and it handles the "I want to see what's happening right now" use case better than spoolctl does.

pueue's daemon is also its weakness for unattended use. `pueued` is a single coordinator process. If it dies, you lose your queue state. There is no automatic retry, no exponential backoff, and no dead-letter state. If a task fails at 3 AM, it stays failed until a human notices.

spoolctl exists for the operator who won't be there when the job fails. If you're a human watching your queue, pueue is the better tool.

## task-spooler

task-spooler (`tsp`) is the venerable serial queue. It runs one job at a time, in order, through a daemon. No retries, no crash recovery, no priorities, no named queues.

Its strength is simplicity: `tsp command` is shorter than any spoolctl invocation. For one-at-a-time serial execution with a human watching, it works.

## nq

nq is the minimalist: no daemon, no database, just lockfiles in a directory. Jobs are serialized by waiting on a lock. No retries, no crash recovery, no concurrent workers, no priorities.

nq's appeal is zero configuration and zero dependencies beyond POSIX. The tradeoff is zero reliability guarantees beyond file-system atomicity.

## Queue libraries (Celery, RQ, huey, litequeue, plainjob, pg-boss)

These are excellent tools that solve a different problem. They queue *functions in an application runtime*, not *commands on a machine*. They assume an app process with a worker fleet and a broker (Redis, RabbitMQ, PostgreSQL).

If you have an application with a worker pool and a message broker, you don't need spoolctl. If you need to queue shell commands on a single machine with no application runtime and no broker, the library shelf doesn't apply.

## Why not cron or systemd timers?

cron and systemd timers are schedulers, not queues. They trigger commands at fixed times but provide no concept of job state, retry on failure, backoff, dead-letter, concurrent claiming, or crash recovery.

spoolctl and cron solve different problems: cron answers "run this at 2 AM every day" and spoolctl answers "run these N commands with retries and crash recovery, letting workers claim them concurrently." A common pattern is using cron to submit jobs to spoolctl.
