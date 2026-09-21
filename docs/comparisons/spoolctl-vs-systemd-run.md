---
title: spoolctl and systemd-run
description: Compare spoolctl with systemd-run transient units. systemd-run can restart, time out, and schedule a command; spoolctl adds a durable queue, default backoff, and macOS.
bucket: concepts
order: 280
---

# spoolctl and systemd-run

**Research date:** 2026-09-21
**spoolctl source baseline:** `main`, commit [`f95600b`](https://github.com/Ozhiaki/spoolctl/tree/f95600bc9b3a93737f93c2e811b55b0129bb3cb6)
**systemd source baseline:** `main`, commit [`e355960`](https://github.com/systemd/systemd/tree/e355960ac1ef10f3c123ae8ce29d654fb4d03b2d)

## Conclusion

`systemd-run` asks the system service manager to run a command as a transient
unit. Because it is systemd, it can already restart on failure, enforce a
runtime limit, apply resource control, and log to the journal. On features it
is the closest OS primitive to spoolctl.

Two things separate them. First, `systemd-run` is not a queue. Each call creates
one independent unit and starts it now, or on a timer. There is no backlog, no
ordering, and no pool of workers draining pending jobs. Second, a transient unit
is runtime only. It has no unit file on disk, it cannot be enabled, and it is
lost on reboot.

Use **systemd-run** on a Linux host with systemd when you want one command
supervised now, with restart and resource control, and you need neither a
durable backlog nor macOS. Use **spoolctl** when you need a durable queue of
many jobs, exponential backoff by default, at-least-once reclaim after a
confirmed-dead worker, machine JSON, and macOS support with no root and no
daemon.

## Scope and architecture

`systemd-run` creates and starts a transient `.service` by default. `--scope`
runs the command in the caller's own context instead. `--on-calendar=` or
`--on-active=` create a transient `.timer` that triggers the service later. The
unit is created through the runtime API; it has no on-disk unit file and cannot
be enabled, so it does not survive a reboot.

The service manager supervises the process: systemd as PID 1 for system units,
or the per-user manager for `--user` units. It applies `Restart=`,
`RuntimeMaxSec=`, cgroup resource limits, and sends the command's standard
output and error to the journal, readable with `journalctl`.

There is one manager. It is not a set of peer workers claiming from a shared
store. A "retry" is the same manager restarting the same unit.

spoolctl has no manager and no journal step. Jobs are rows in one SQLite file.
Any number of peer workers claim jobs, each in one transaction, and write a
heartbeat while a job runs. A job is reclaimed only after its owner process is
confirmed dead. See [Guarantees](/docs/guarantees/).

## Capability comparison

| Area | systemd-run (transient unit) | spoolctl at `f95600b` |
| --- | --- | --- |
| Primary job | Run one command as a supervised transient unit, now or on a timer. | Run commands that must survive the death of any runner. |
| Coordinator | The systemd service manager (system, or `--user`). | None. One SQLite file. |
| Is it a queue? | No. One unit per call; no backlog or worker pool. | Yes. Rows drained by any worker. |
| Persistence | Runtime only. No on-disk unit; lost on reboot. | Durable in SQLite; survives reboot. |
| Retry on failure | Opt-in: `-p Restart=on-failure`. | Built in. |
| Retry backoff | Fixed `RestartSec` (default 100 ms); exponential only if `RestartSteps=` and `RestartMaxDelaySec=` are set. | Exponential by default. |
| Retry budget / failed state | Start-rate limit (`StartLimitBurst` per `StartLimitIntervalSec`); unit enters `failed`. | `max-retries`, then `dead`. |
| Per-job timeout | Yes: `-p RuntimeMaxSec=`. | Yes. Default 300 s. Kills the process group. |
| Concurrency limit | None built in. Each unit is independent; slices bound resources, not job count. | Lanes, each with an optional fleet-wide `--slots` ceiling. |
| Reclaim after the runner dies | The same manager restarts per `Restart=`; no dead-owner handshake, since there are no separate workers. | Reclaimed and run again only after the owner is confirmed dead. |
| Delayed start | Yes: `--on-calendar`, `--on-active` (transient timer). | Yes: `--after`, `--at`. |
| Output | Journal; `journalctl -u <unit>`. | Files per attempt; `output` verb from any process. |
| Machine interface | D-Bus; `systemctl show` properties; `journalctl -o json`. | JSON envelope on every verb, stable error codes, schemas. |
| Install | Linux with systemd as PID 1. Root for system units; `--user` for a user manager. | Python 3 standard library only. No root. |
| Platforms | Linux only. | macOS, Linux. |

## What systemd-run does well

- It reuses the whole service manager. Restart policy, timeout, resource control
  through cgroups, sandboxing, and journal logging are each one `-p` flag away.
- A transient timer replaces ad-hoc cron for one-off scheduling:
  `systemd-run --on-calendar='*-*-* 02:00' ...`.
- Output goes to the journal automatically, with metadata, queryable by unit.
- `--scope` runs a command in your own session under systemd supervision.
- It is already installed on any systemd Linux host.

## What spoolctl does differently

- spoolctl is a queue. It holds a backlog and drains it through workers under a
  slot limit. `systemd-run` starts one unit per call.
- spoolctl's queue is a durable file. Pending jobs survive a reboot. A transient
  unit does not.
- spoolctl retries with exponential backoff by default. `systemd-run` restarts
  at a fixed interval unless you configure the backoff steps.
- spoolctl reclaims a job only after the owner process is confirmed dead, so two
  workers never run one attempt. systemd has one manager, so the question does
  not arise there, and there is no cross-process claim model.
- spoolctl runs on macOS and needs no root, no systemd, and no daemon.
  `systemd-run` is Linux and systemd only.

## Composition

Use a transient timer to start a spoolctl worker or submit a job. systemd owns
the schedule and supervision of that one call; spoolctl owns the durable queue,
backoff, and concurrency.

```
systemd-run --on-calendar='*-*-* 02:00' spoolctl add -- ./job.sh
```

You can also run a spoolctl worker as a service with `Restart=always`, so
systemd keeps the worker alive while spoolctl manages the jobs.

## Limits and claims not to make

- Do not say `systemd-run` cannot retry or time out. `-p Restart=on-failure`
  retries; `-p RuntimeMaxSec=` times out.
- Do not say `systemd-run` has no backoff. It restarts at a fixed interval by
  default, and does exponential backoff when `RestartSteps=` and
  `RestartMaxDelaySec=` are set.
- Do not call `systemd-run` a queue. It creates one unit per invocation; there
  is no shared backlog or worker pool.
- Do not say a transient unit survives a reboot. It is created through the
  runtime API, cannot be enabled, and is lost on reboot.
- Do not compare on macOS. `systemd-run` does not exist there.
- Do not say systemd loses a job's output on a crash. The journal keeps what was
  written.

## Sources

- [`systemd-run(1)`](https://github.com/systemd/systemd/blob/e355960ac1ef10f3c123ae8ce29d654fb4d03b2d/man/systemd-run.xml#L64-L86) — transient service, `--scope`, and `--on-calendar`/`--on-active` timers; output to the journal.
- [`systemd.service(5)` `Restart=`](https://github.com/systemd/systemd/blob/e355960ac1ef10f3c123ae8ce29d654fb4d03b2d/man/systemd.service.xml#L876-L911) — default `no`; `on-failure` restarts on a non-zero exit or a signal (including SIGKILL). Backoff: [`RestartSec`/`RestartSteps`/`RestartMaxDelaySec`](https://github.com/systemd/systemd/blob/e355960ac1ef10f3c123ae8ce29d654fb4d03b2d/man/systemd.service.xml#L611-L660). Timeout: [`RuntimeMaxSec`](https://github.com/systemd/systemd/blob/e355960ac1ef10f3c123ae8ce29d654fb4d03b2d/man/systemd.service.xml#L809).
- [`systemd.unit(5)` start-rate limit](https://github.com/systemd/systemd/blob/e355960ac1ef10f3c123ae8ce29d654fb4d03b2d/man/systemd.unit.xml#L1214-L1230) — `StartLimitIntervalSec`/`StartLimitBurst`.
- [`systemctl(1)` transient state](https://github.com/systemd/systemd/blob/e355960ac1ef10f3c123ae8ce29d654fb4d03b2d/man/systemctl.xml#L1138-L1139) — a transient unit is created with the runtime API and may not be enabled.
- [spoolctl guarantees](https://github.com/Ozhiaki/spoolctl/blob/f95600bc9b3a93737f93c2e811b55b0129bb3cb6/docs/guarantees.md?plain=1)
