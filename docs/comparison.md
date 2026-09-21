---
title: Comparison
description: Compare spoolctl with pueue, task-spooler, nq, GNU parallel, huey, and RQ. Links to a detailed, source-pinned page for each tool.
bucket: project
order: 10
---

# Comparison

spoolctl is a queue for shell commands with no daemon and no broker. Its main
difference from other local queues is one question: **what happens to a
running job when its runner gets SIGKILL, and nobody is watching?** In
spoolctl, another worker confirms that the owner process is dead, then runs
the job again.

This page is a short summary. Each tool has a detailed page with pinned
sources. The [local job queue landscape](/docs/comparisons/local-job-queue-landscape/)
explains the tool classes.

## Summary table

|  | spoolctl | pueue | task-spooler | nq | GNU parallel | huey | RQ |
|---|---|---|---|---|---|---|---|
| Jobs are shell commands | yes | yes | yes | yes | yes | no (Python) | no (Python) |
| No daemon or server | yes | no (`pueued`) | no (server) | yes | yes (runner process) | needs consumer | needs Redis |
| Queue state survives coordinator death | yes | yes (`state.json`) | no | reboot drops queued jobs | job log, for resume | yes | yes |
| Running job runs again after runner SIGKILL | yes | no (marked `Killed`) | no | no | no | no (lost) | yes, if retries left |
| Automatic retry | yes, with backoff | no | no | no | yes, no backoff | yes, with backoff | yes, with intervals |
| Per-job timeout | yes, process group | no | no | no | yes | yes | yes |
| Concurrent jobs | yes, many workers | yes, one daemon | yes, one server | no | yes, one runner | yes | yes |
| Dependencies | no | yes | yes | no | n/a | pipelines | yes |
| Runtime needs | Python 3 stdlib | Rust binary | C binary | C binary | Perl | Python package | Python package + Redis |

## Detailed pages

- [spoolctl and pueue](/docs/comparisons/spoolctl-vs-pueue/): the best tool
  for a person who watches long-running commands. It restores queue state
  after a crash, but marks running tasks `Killed` and pauses groups.
- [spoolctl and task-spooler](/docs/comparisons/spoolctl-vs-task-spooler/):
  short commands, slots, dependencies, and GPU allocation. The queue lives in
  server memory.
- [spoolctl and nq](/docs/comparisons/spoolctl-vs-nq/): no daemon, like
  spoolctl. Strictly serial, and a queued job is a waiting process.
- [spoolctl and GNU parallel](/docs/comparisons/spoolctl-vs-gnu-parallel/):
  the best batch runner. It has retries, timeouts, and resume, but it is a
  batch, not a durable queue.
- [spoolctl and huey](/docs/comparisons/spoolctl-vs-huey/): a Python task
  queue with a SQLite backend. A task interrupted by SIGKILL is lost.
- [spoolctl and RQ](/docs/comparisons/spoolctl-vs-rq/): a Python task queue
  on Redis. It recovers abandoned jobs through registry cleanup.

## Why not cron or systemd timers?

cron and systemd timers are schedulers, not queues. They start commands at
fixed times. They keep no job state, and they have no claim, retry, or
recovery.

cron answers "run this at 2 AM every day." spoolctl answers "run these jobs,
retry the failures, and recover from dead workers." A common pattern is cron
that submits jobs to spoolctl.

## What spoolctl does not do

spoolctl has no job dependencies, recurring schedules, TUI, multi-host
workers, or Windows support. See [Limits](/docs/limits/) and
[Guarantees](/docs/guarantees/).
