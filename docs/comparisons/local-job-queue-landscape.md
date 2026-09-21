---
title: Local Job Queue Landscape
description: Compare command queues, parallel runners, and task-queue libraries with spoolctl, a daemonless crash-safe shell job queue.
bucket: concepts
order: 200
---

# Local job queue landscape

**Research date:** 2026-09-21
**spoolctl source baseline:** `main`, commit [`f95600b`](https://github.com/Ozhiaki/spoolctl/tree/f95600bc9b3a93737f93c2e811b55b0129bb3cb6)

## Conclusion

Many tools queue work on one machine. Few of them keep a job safe when the
process that runs it is killed. spoolctl is a queue for shell commands with no
daemon and no broker. All workers coordinate through one SQLite file. If a
worker dies mid-job, another worker confirms that the owner process is gone,
then runs the job again. Failed jobs retry with backoff, then stop in a `dead`
state.

This index lists the tools we compared, what they share with spoolctl, and
where they differ. It makes no performance claim. Each linked page pins the
source version it describes.

## Four tool classes

| Class | Main job | Coordinator | Examples |
| --- | --- | --- | --- |
| **Interactive command queue** | Run shell commands in order for a person who watches them. | A daemon, or file locks. | pueue, task-spooler, nq |
| **Parallel batch runner** | Run one command over many inputs, in parallel, in one invocation. | The runner process. | GNU parallel |
| **Application task queue** | Run functions from an application codebase on worker processes. | A broker or database, plus the application. | huey, RQ, Celery |
| **Unattended command queue** | Run shell commands that must survive the death of any runner. | The database file. | spoolctl |

The deciding question is simple. **What happens to a running job when its
runner gets SIGKILL, and nobody is watching?**

## Comparison index

| Tool | Class | Overlap with spoolctl | Main difference | Page |
| --- | --- | --- | --- | --- |
| [pueue](https://github.com/Nukesor/pueue) | Interactive command queue | Shell commands, groups with parallel limits, persisted state, JSON status | A daemon owns execution. A task running at a daemon crash becomes `Killed`; it does not run again. | [Compare](/docs/comparisons/spoolctl-vs-pueue/) |
| [task-spooler](https://github.com/justanhduc/task-spooler) | Interactive command queue | Shell commands, slots, dependencies, GPU slots | The queue lives in the server's memory. Nothing restores it after the server dies. | [Compare](/docs/comparisons/spoolctl-vs-task-spooler/) |
| [nq](https://github.com/leahneukirchen/nq) | Daemonless command queue | No daemon, filesystem coordination, per-job log files | Strictly serial. A queued job is a waiting process, so a reboot drops the queue. No retry. | [Compare](/docs/comparisons/spoolctl-vs-nq/) |
| [GNU parallel](https://www.gnu.org/software/parallel/) | Parallel batch runner | Retries, per-job timeout, job log, resume, SQL worker mode | Retries are immediate, with no backoff. Its manual says more than one SQL worker may run a job more than once. | [Compare](/docs/comparisons/spoolctl-vs-gnu-parallel/) |
| [huey](https://github.com/coleifer/huey) | Application task queue | SQLite backend, retries with backoff, priorities, delays | Jobs are Python functions. A task interrupted by SIGKILL is lost. | [Compare](/docs/comparisons/spoolctl-vs-huey/) |
| [RQ](https://github.com/rq/rq) | Application task queue | Crash recovery of abandoned jobs, retries, registries | Jobs are Python functions. Needs Redis. | [Compare](/docs/comparisons/spoolctl-vs-rq/) |
| at, batch | Time-based command scheduler | Shell commands, delayed start, durable spool files, base-system install | Runs a job once. No retry, no timeout, no concurrency limit, no JSON. Output is mailed and deleted. | [Compare](/docs/comparisons/spoolctl-vs-at/) |
| [litequeue](https://github.com/litements/litequeue) | SQLite message queue | Persistent SQLite queue with claims | A message queue. It stores messages; it runs no commands. | Context only |
| cron, systemd timers | Scheduler | Starts commands on a schedule | No job state, retry, or claim. A common pattern: cron submits jobs to spoolctl. | Context only |

## Summary table

"Survives runner SIGKILL" means the job runs again after the process that ran
it is killed. It does not mean the queue file survives, which is true for most
of these tools.

| | spoolctl | pueue | task-spooler | nq | GNU parallel | huey | RQ |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Unit of work | shell command | shell command | shell command | shell command | shell command | Python function | Python function |
| Coordinator | SQLite file | daemon | daemon | file locks | runner process | consumer + storage | worker + Redis |
| Queue state survives coordinator death | yes | yes | no | queued jobs lost on reboot | job log, for resume | yes | yes |
| Running job survives runner SIGKILL | yes, re-run | no, marked `Killed` | no | no | no | no, lost | yes, if retries left |
| Automatic retry | yes | no | no | no | yes | yes | yes |
| Retry backoff | exponential | no | no | no | no | yes | intervals |
| Dead / failed state | `dead` | `Failed` | exit level in list | optional `NQFAILDIR` | job log | error stored as result | `FailedJobRegistry` |
| Per-job timeout | yes, process group | no | no | no | yes | yes (cooperative with threads) | yes |
| Concurrent independent runners | yes | no (one daemon) | no (one server) | no | SQL mode only | yes | yes |
| Runtime dependencies | Python 3 stdlib | Rust binary | C binary | C binary | Perl | Python package | Python package + Redis |
| Platforms | macOS, Linux | Linux, macOS, Windows | Unix-like | POSIX | POSIX | Python; no process workers on Windows | POSIX; Windows via `SpawnWorker` |

Each cell is explained, with sources, on the tool's own page.

## Product position

spoolctl is a single-machine CLI. It makes these claims, each with a
mechanism and a test in [Guarantees](/docs/guarantees/):

- Two live workers never own the same attempt at the same time.
- A job is reclaimed only after the owner process is confirmed dead.
  "Inconclusive" means "leave it."
- Execution is at-least-once. A command can run again after an ambiguous
  crash. Make jobs idempotent.
- Timeouts kill the whole process group.

spoolctl does not have job dependencies, recurring schedules, a TUI,
multi-host workers, or Windows support. See [Limits](/docs/limits/).

## Sources and update policy

Each comparison page lists its own pinned sources. Check the named version or
commit again before a page changes. Add a tool to this index only after a
source review.
