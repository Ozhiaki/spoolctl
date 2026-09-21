---
title: spoolctl and GNU parallel
description: Compare spoolctl, a durable job queue, with GNU parallel, the parallel batch runner with retries, timeouts, a job log, and SQL worker mode.
bucket: concepts
order: 240
---

# spoolctl and GNU parallel

**Research date:** 2026-09-21
**spoolctl source baseline:** `main`, commit [`f95600b`](https://github.com/Ozhiaki/spoolctl/tree/f95600bc9b3a93737f93c2e811b55b0129bb3cb6)
**GNU parallel source baseline:** release 20260722, commit `4359ad0` in the [Savannah git repository](https://git.savannah.gnu.org/cgit/parallel.git)

## Conclusion

Use [**GNU parallel**](https://www.gnu.org/software/parallel/) to run one
command over many inputs, fast, in one invocation. It is the right tool for a
batch that you start, watch, and finish. It can spread work over SSH hosts.
spoolctl cannot.

Use **spoolctl** when jobs arrive over time, from many submitters, and must
run even if every runner dies. GNU parallel is a runner process. Its queue is
its input. When the runner dies, its running jobs die too. You can resume from
its job log, but a person or script must start it again.

## Scope and architecture

One `parallel` process reads inputs, builds commands, and runs up to `--jobs`
of them at once. It has strong batch controls:

- `--retries n` runs a failed job again, up to *n* times. On remote hosts it
  prefers a host where the job has not failed.
- `--timeout` kills a job after a duration. The duration can be a percentage
  of the median run time of successful jobs. The kill follows `--term-seq`,
  by default TERM, TERM, TERM, then KILL within 375 ms.
- `--joblog` writes one line per job with exit status and signal.
- `--resume`, `--resume-failed`, and `--retry-failed` read the job log and
  continue a batch after it stops.

GNU parallel also has two modes that look like a queue:

- `sem` (`parallel --semaphore`) is a counting semaphore. It limits how many
  commands run at once across calls.
- `--sql-master` writes jobs to a database table, SQLite included.
  `--sql-worker` processes run them. The manual states: "If you have more than
  one --sqlworker jobs may be run more than once."

spoolctl is a queue first. Jobs are rows in SQLite. Any number of workers
claim them. A claim is one `BEGIN IMMEDIATE` transaction, so two live workers
never own the same attempt. See [Guarantees](/docs/guarantees/).

## Capability comparison

| Area | GNU parallel 20260722 | spoolctl at `f95600b` |
| --- | --- | --- |
| Primary job | Run a command over many inputs, in parallel. | Run commands that must survive the death of any runner. |
| Coordinator | The `parallel` process. | None. One SQLite file. |
| Jobs submitted over time | `sem`, or `--sql-master` then workers. | Yes. `spoolctl add` at any time, from any process. |
| Running job after runner death | Killed with the runner. Rerun with `--resume` or `--resume-failed`. | Reclaimed and run again after the owner is confirmed dead. |
| Several runners on one queue | `--sql-worker`; the manual warns jobs may run more than once. | Yes. One claim per attempt. |
| Retry | `--retries n`. The manual documents no delay between tries. | Exponential backoff, capped at 60 s, then `dead`. |
| Per-job timeout | Yes, fixed or % of median run time. | Yes. Default 300 s. Kills the process group. |
| Job record | `--joblog` TSV: exit status, signal, run time. | Job and attempt rows; JSON on every verb. |
| Remote execution | Yes, over SSH. | No. One machine. |
| Input handling | Rich: argument lists, pipes, replacement strings. | One command per job. |
| Install | Perl script. | Python 3 standard library only. |

## What GNU parallel does well

- It is the fastest way to run a large batch on all cores, or on many hosts.
- Its input syntax is powerful. One line can replace a script.
- Its timeout adapts to the batch: `--timeout 200%` kills outliers.
- Its job log makes a stopped batch resumable.
- Its manual is precise, including about its own limits.

The pinned manual source is `src/parallel.pod` in the Savannah repository.

## What spoolctl does differently

GNU parallel treats a batch as one run. spoolctl treats each job as a durable
record. A job waits in the database until a worker takes it. That worker can
start hours later, after a reboot.

spoolctl confirms that a dead worker is really dead before it reclaims a job.
The GNU parallel SQL mode does not claim that guarantee.

spoolctl spaces its retries with backoff. That helps when a failure comes from
a busy service or a full disk.

## Composition

The tools work well together:

- A spoolctl job can run `parallel` to fan out one step.
- `parallel` can call `spoolctl add` for each input, to turn a batch into
  durable jobs.

## Limits and claims not to make

- Do not say GNU parallel has no retry or timeout. It has both.
- Do not say GNU parallel cannot resume. The job log supports it.
- Do not say GNU parallel cannot use SQLite. `--sql-master` accepts a
  `sqlite3://` DBURL.
- Do not claim spoolctl is faster. This page makes no performance claim.
- Do not claim spoolctl runs jobs on remote hosts.

## Sources

- [GNU parallel home page](https://www.gnu.org/software/parallel/)
- [GNU parallel manual](https://www.gnu.org/software/parallel/parallel.html): `--joblog`, `--resume`, `--retries`, `--semaphore`, `--sql-master`, `--sql-worker`, `--term-seq`, `--timeout`
- [GNU parallel Savannah repository](https://git.savannah.gnu.org/cgit/parallel.git), release 20260722, commit `4359ad0710c1b7465d7b83bb9b32b49688e93ca9`
- [spoolctl guarantees](https://github.com/Ozhiaki/spoolctl/blob/f95600bc9b3a93737f93c2e811b55b0129bb3cb6/docs/guarantees.md?plain=1)
