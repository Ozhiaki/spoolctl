---
title: spoolctl and at/batch
description: Compare spoolctl with at and batch, the base-system command schedulers. Both run shell commands later; they differ on retries, timeouts, concurrency, and status.
bucket: concepts
order: 270
---

# spoolctl and at/batch

**Research date:** 2026-09-21
**spoolctl source baseline:** `main`, commit [`f95600b`](https://github.com/Ozhiaki/spoolctl/tree/f95600bc9b3a93737f93c2e811b55b0129bb3cb6)
**at/batch source baselines:** macOS lineage — FreeBSD `at` and `atrun`, commit [`a6deeaa`](https://github.com/freebsd/freebsd-src/tree/a6deeaa2fb3b28a61ae56a85f2275f67fcf23262); Linux — Debian `at` (`atd`), commit [`282d01d`](https://salsa.debian.org/debian/at/-/tree/282d01da5a738af1b9b0b724f0b989e51bfe3ba3)

## Conclusion

`at` and `batch` are the original Unix way to run a command later. `at` runs it
at a set time. `batch` runs it when the machine is idle. Both save the command
as a spool file, and a periodic runner executes it once.

Use **at** or **batch** for a one-off command with nothing to install. It is on
almost every Unix box. `echo 'make backup' | at 2am` is the whole setup.

Use **spoolctl** when a job must retry on failure, run under a timeout, run in
parallel with a limit, or report machine-readable status. `at` and `batch` do
none of these. They run a job once and mail its output. There is no retry, no
timeout, no concurrency limit, and no JSON.

## Scope and architecture

`at` writes the job as a spool file whose name encodes a queue letter and a job
number, under `/var/at/jobs` on macOS or `/var/spool/cron/atjobs` on Linux. The
queue letter sets the job's `nice` value. `a` is the default `at` queue; `b` is
the default `batch` queue. `batch` uses an uppercase queue letter.

The spool file is not run by `at` itself. A separate runner is invoked on a
schedule and executes due jobs: `atd` on Linux, `atrun` on macOS. On macOS
`atrun` is a launchd job, `com.apple.atrun`, that is **disabled by default**. No
`at` job runs until an admin enables it.

The runner runs each due job once. The BSD/macOS `atrun` clears the file's
execute bit before it forks the job; its scan only runs files that still have
the bit, so a job interrupted by a crash is never run again, and the spent file
is unlinked. The Linux `atd` hard-links the job to a `=` lock, runs it, then
unlinks the original once it is committed to the run. Neither runner confirms
that a dead job's process is gone. `batch` runs at most one job per runner pass,
oldest first, and only while the load average is below a threshold (1.5 times
the CPU count by default). The job runs as `/bin/sh`. Its output is captured,
mailed to the user, and then deleted.

spoolctl has no periodic runner and no mail step. Jobs are rows in one SQLite
file. A worker claims a job in one transaction and writes a heartbeat while it
runs. Another worker reclaims the job only after the owner process is confirmed
dead. See [Guarantees](/docs/guarantees/).

## Capability comparison

| Area | at / batch | spoolctl at `f95600b` |
| --- | --- | --- |
| Primary job | Run a shell command once: at a time (`at`), or when load is low (`batch`). | Run commands that must survive the death of any runner. |
| Coordinator | A periodic runner: `atd` (Linux) or `atrun` via launchd (macOS). | None. One SQLite file. |
| Runner on macOS | `com.apple.atrun`, disabled by default. | Workers are plain processes. |
| Queue state after a reboot | Durable. Spool files remain; the next runner pass executes due jobs. | Durable in SQLite. |
| Running job after the runner dies | macOS: not re-run (execute bit cleared first). Linux: a job caught before commit can be rescheduled once. No dead-owner check either way. | Reclaimed and run again only after the owner is confirmed dead. |
| Concurrency | `at`: every due job launches. `batch`: one job per pass, gated on load. No per-queue limit. | Lanes, each with an optional fleet-wide `--slots` ceiling. |
| Automatic retry | No. | Yes. Exponential backoff, then `dead`. |
| Per-job timeout | No. | Yes. Default 300 s. Kills the process group. |
| Failure record | None. Output, including errors, is mailed and then deleted. | `failed` and `dead` states, attempt history, error codes. |
| Delayed start | Yes: `at TIME`. | Yes: `--after`, `--at`. |
| Priority / dependencies | Queue letter sets `nice` only. No dependencies. | `--priority` at submit; lanes. No dependencies. |
| Output | Mailed to the user, then unlinked. Not retrievable by another process. | Files per attempt; `output` verb from any process. |
| Machine interface | `atq` lists, `atrm` removes, `at -c` prints the script. No JSON. | JSON envelope on every verb, stable error codes, schemas. |
| Install | Base system. On macOS, enable `com.apple.atrun` first. | Python 3 standard library only. |
| Platforms | Unix. macOS ships it (runner off by default); Linux via the `at` package. | macOS, Linux. |

## What at/batch do well

- Nothing to install. `at` is part of the base system on almost every Unix box.
- One line schedules a job: `echo 'make backup' | at 2am`.
- `batch` is a neat idle-time throttle. It holds work until the load drops.
- A queued job is a plain spool file. It survives a reboot with no daemon
  running, and the next runner pass picks it up.
- Output arrives by mail, so a person needs no polling to read it.

## What spoolctl does differently

- spoolctl retries. `at` and `batch` run a job once; a failed command is just
  mailed output.
- spoolctl bounds a job with a timeout and kills its process group. `at` and
  `batch` let a job run forever.
- spoolctl runs many jobs at once under a slot ceiling. `at` launches every due
  job; `batch` runs one per pass.
- spoolctl exposes JSON and stable error codes. `at` and `batch` expose spool
  files and mail.
- spoolctl confirms the owner process is dead before it re-runs a job. `at` and
  `batch` have no such check.

## Composition

The tools compose in one direction. Use `at` or `cron` to submit into spoolctl,
so the OS owns the schedule and spoolctl owns retry, timeout, and concurrency.

```
echo 'spoolctl add -- ./job.sh' | at 2am
```

They do not otherwise integrate.

## Limits and claims not to make

- Do not say `at`/`batch` lose the queue on a reboot. Spool files persist, and
  the next runner pass runs the due jobs. For a *queued* job, `at` is more
  durable than a process-based queue such as nq.
- Do not say `at` re-runs a failed job. It never retries. A nonzero exit is
  mailed, not retried.
- Do not claim the two implementations behave the same on a mid-run crash.
  macOS/BSD `atrun` never re-runs; Linux `atd` can reschedule a job caught
  before it commits. Neither confirms the owner is dead.
- Do not say `batch` runs jobs in parallel. It runs at most one job per runner
  pass.
- Do not say `at` needs a persistent daemon. The runner is invoked
  periodically and exits.

## Sources

- [FreeBSD `atrun.c`](https://github.com/freebsd/freebsd-src/blob/a6deeaa2fb3b28a61ae56a85f2275f67fcf23262/libexec/atrun/atrun.c#L146-L162) — clears the execute bit before the run; the scan and `batch`/load logic are at [L536-L567](https://github.com/freebsd/freebsd-src/blob/a6deeaa2fb3b28a61ae56a85f2275f67fcf23262/libexec/atrun/atrun.c#L536-L567), the default load at [L84](https://github.com/freebsd/freebsd-src/blob/a6deeaa2fb3b28a61ae56a85f2275f67fcf23262/libexec/atrun/atrun.c#L84).
- [FreeBSD `at.c`](https://github.com/freebsd/freebsd-src/blob/a6deeaa2fb3b28a61ae56a85f2275f67fcf23262/usr.bin/at/at.c) — queue letters and the default `at`/`batch` queues.
- [Debian `atd.c`](https://salsa.debian.org/debian/at/-/blob/282d01da5a738af1b9b0b724f0b989e51bfe3ba3/atd.c#L320-L336) — hard-link lock and the "restart if something went wrong" window.
- `at(1)` / `batch(1)` manual pages: `batch` runs "when the load average drops below 1.5 times number of active CPUs, or the value specified in the invocation of atrun."
- macOS `/System/Library/LaunchDaemons/com.apple.atrun.plist`: `Disabled = 1`.
- [spoolctl guarantees](https://github.com/Ozhiaki/spoolctl/blob/f95600bc9b3a93737f93c2e811b55b0129bb3cb6/docs/guarantees.md?plain=1)
