---
title: spoolctl and nq
description: Compare spoolctl with nq, the daemonless flock-based command queue. Both need no daemon; they differ on concurrency, retries, and crash recovery.
bucket: concepts
order: 230
---

# spoolctl and nq

**Research date:** 2026-09-21
**spoolctl source baseline:** `main`, commit [`f95600b`](https://github.com/Ozhiaki/spoolctl/tree/f95600bc9b3a93737f93c2e811b55b0129bb3cb6)
**nq source baseline:** 1.0 (2024-07-03), commit [`8bad1d0`](https://github.com/leahneukirchen/nq/tree/8bad1d011bc1da3c7c8cf58eb7cf19481a73de9d)

## Conclusion

nq is the closest relative of spoolctl in spirit. Neither tool has a daemon.
Both use the filesystem to coordinate. Both are small.

Use [**nq**](https://github.com/leahneukirchen/nq) to run commands one after
another, in the background, with no setup. It is a single C file and a good
`nohup` replacement.

Use **spoolctl** when jobs must run in parallel, retry on failure, or survive
a reboot. nq runs exactly one job at a time per queue directory. A queued nq
job is a waiting process, so a reboot or kill of that process drops it. nq
never retries.

## Scope and architecture

Each `nq` call creates a log file named `,TIMESTAMP.PID` and locks it with
`flock(2)`. The job waits until every earlier lock file in the directory is
unlocked, then runs. The lock is shared with the job process, so it releases
when the job ends. The README says: "Exclusive execution is maintained
strictly."

When the job ends, nq appends `[exited with status N.]` or
`[killed by signal N.]` to the log. With `$NQDONEDIR` set, it moves a
successful log there. With `$NQFAILDIR` set, it moves a log with a nonzero
exit there. A job killed by a signal stays in place. See the pinned
[exit handling code](https://github.com/leahneukirchen/nq/blob/8bad1d011bc1da3c7c8cf58eb7cf19481a73de9d/nq.c#L264-L289).

spoolctl also has no daemon, but it keeps job state as rows in one SQLite
file, not as processes. A queued job is a row. It waits for any worker, now or
after a reboot. See [Guarantees](/docs/guarantees/).

## Capability comparison

| Area | nq 1.0 | spoolctl at `f95600b` |
| --- | --- | --- |
| Primary job | Run command lines in sequence, in the background. | Run commands that must survive the death of any runner. |
| Coordinator | None. `flock(2)` on log files in `$NQDIR`. | None. One SQLite file. |
| Concurrency | One job at a time per directory. | Many workers; lanes with optional `--slots` ceilings. |
| Queued job | A waiting `nq` process. | A database row. |
| Queued jobs after a reboot | Never start. Logs remain. | Claimed by the next worker. |
| Running job after its runner dies | Not run again. | Reclaimed and run again after the owner is confirmed dead. |
| Retry | No. Resubmit by hand with `sh $jobid`. | Automatic, exponential backoff, then `dead`. |
| Failure record | Exit status in the log; optional move to `$NQFAILDIR`. | `failed` and `dead` states, attempt history, error codes. |
| Per-job timeout | No. | Yes. Default 300 s. Kills the process group. |
| Output | One log file per job; `nqtail` follows it. | Files per attempt; `output` verb from any process. |
| Machine interface | Log files and job IDs. | JSON envelope on every verb, stable error codes, schemas. |
| Install | One C binary. A shell version, `nq.sh`, also exists. | Python 3 standard library only. |
| Platforms | POSIX.1-2008 with working `flock(2)`. | macOS, Linux. |

## What nq does well

- It needs almost nothing. One small binary, no config, no database.
- Its order is strict and needs no polling.
- Any directory can be a queue. The README encourages wrappers that set
  `$NQDIR` for different purposes.
- A log file doubles as a resubmit script. `sh ,TIMESTAMP.PID` runs the job
  again.
- Its README has a fair comparison with `at`, `batch`, and task-spooler.

## What spoolctl does differently

nq ties a queued job to a live process. That is why it needs no daemon, and
also why a reboot drops the queue. spoolctl ties a job to a row. Any worker
started later can run it.

spoolctl runs jobs in parallel. Several workers can drain one queue. Lanes
and slot ceilings limit how many run at once.

spoolctl retries without help and records each attempt. nq records one exit
status and stops.

## Composition

The tools do not integrate. For strictly serial work that must survive a
reboot, run one spoolctl worker per lane with `--slots 1`.

## Limits and claims not to make

- Do not say nq needs a daemon. It needs none.
- Do not say nq has no failure record. The log states the exit status, and
  `$NQFAILDIR` separates failed jobs.
- Do not say nq loses running output on a crash. The log file stays on disk.
- Do not claim spoolctl is smaller or simpler to install than nq.

## Sources

- [nq README](https://github.com/leahneukirchen/nq/blob/8bad1d011bc1da3c7c8cf58eb7cf19481a73de9d/README.md?plain=1)
- [nq exit handling](https://github.com/leahneukirchen/nq/blob/8bad1d011bc1da3c7c8cf58eb7cf19481a73de9d/nq.c#L264-L289)
- [nq release notes](https://github.com/leahneukirchen/nq/blob/8bad1d011bc1da3c7c8cf58eb7cf19481a73de9d/NEWS.md?plain=1)
- [spoolctl guarantees](https://github.com/Ozhiaki/spoolctl/blob/f95600bc9b3a93737f93c2e811b55b0129bb3cb6/docs/guarantees.md?plain=1)
