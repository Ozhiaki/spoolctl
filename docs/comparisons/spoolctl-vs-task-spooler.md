---
title: spoolctl and task-spooler
description: Compare spoolctl durable, crash-safe job execution with task-spooler (ts, tsp), the in-memory command spooler.
bucket: concepts
order: 220
---

# spoolctl and task-spooler

**Research date:** 2026-09-21
**spoolctl source baseline:** `main`, commit [`f95600b`](https://github.com/Ozhiaki/spoolctl/tree/f95600bc9b3a93737f93c2e811b55b0129bb3cb6)
**task-spooler source baseline:** GPU Task Spooler v2.0.0, commit [`1522ab0`](https://github.com/justanhduc/task-spooler/tree/1522ab0fa22a3d45f425a3d1fab59774af031e7c)

There are two task-spoolers. Lluís Batlle i Rossell wrote the original. Many
Linux distributions package it as `tsp`. The GitHub fork by justanhduc, "GPU
Task Spooler," adds GPU allocation and is the source reviewed here. The core
queue model is the same in both.

## Conclusion

Use [**task-spooler**](https://github.com/justanhduc/task-spooler) for quick,
short-lived queues on a workstation or a shared GPU server. Its command line is
very short, it allocates free GPUs, and it can run jobs after other jobs end.

Use **spoolctl** when the queue must survive a crash. task-spooler keeps its
job list in the memory of its server process. If the server gets SIGTERM, it
can write the list to a file, but nothing reads that file back. If the server
gets SIGKILL, or the machine reboots, the queue is gone. spoolctl keeps every
job in SQLite, reclaims jobs from dead workers, and retries failures with
backoff.

## Scope and architecture

The first `ts` command starts a server process. Later `ts` commands are
clients. The server holds the job list and starts jobs. It runs as many jobs
at once as its slot count, set with `-S` or `TS_SLOTS`.

The SIGTERM handler writes the job list to `TS_SAVELIST`, if that variable is
set, and then exits. See the pinned
[server signal handler](https://github.com/justanhduc/task-spooler/blob/1522ab0fa22a3d45f425a3d1fab59774af031e7c/server.c#L78-L97).
The help text describes that file as the place that "will store the list, if
the server dies." The reviewed source has no code that restores it.

spoolctl has no server. Every worker is a peer and reads the same SQLite file.
See [Guarantees](/docs/guarantees/).

## Capability comparison

| Area | task-spooler v2.0.0 | spoolctl at `f95600b` |
| --- | --- | --- |
| Primary job | Spool commands on one machine, with GPU allocation. | Run commands that must survive the death of any runner. |
| Coordinator | An auto-started server process. | None. The SQLite file. |
| Queue state after server SIGTERM | Optional text dump to `TS_SAVELIST`. No restore. | Not applicable. No server. |
| Queue state after SIGKILL or reboot | Lost. | Durable in SQLite. |
| Retry of a failed job | No. | Automatic, exponential backoff, then `dead`. |
| Per-job timeout | No. | Yes. Default 300 s. Kills the process group. |
| Parallelism | `-S` slots per server; `-N` slots per job. | Lanes with an optional fleet-wide `--slots` ceiling. |
| Dependencies | `-D` after given jobs end; `-W` after they succeed; `-d` after the last job. | No. |
| Ordering | `-u` moves a job first; `-U` swaps two jobs. | `--priority` at submit time. |
| GPU allocation | Yes: `-G` count, `--gpu_indices` list. | No. |
| Output | One output file per job; `-t`, `-c` to view. | Files per attempt; `output` verb from any process. |
| Machine interface | `-M json` serializes the job list. | JSON envelope on every verb, stable error codes, schemas. |
| Install | C binary. | Python 3 standard library only. |

## What task-spooler does well

- It is fast to use. `ts make` queues a build.
- Its GPU mode allocates free GPUs to jobs. spoolctl has no GPU awareness.
  Lanes with `--slots 1` can serialize GPU work, but they do not pick a GPU.
- It has job dependencies, including "run only if these jobs succeeded."
- It can mail job output and call a hook when a job ends.

The pinned [help text](https://github.com/justanhduc/task-spooler/blob/1522ab0fa22a3d45f425a3d1fab59774af031e7c/main.c#L484-L555) lists these options.

## What spoolctl does differently

task-spooler assumes a person will notice a lost queue. spoolctl assumes no
one will. Its job state is always on disk, in one file, and any worker can
recover a job whose owner died.

spoolctl retries a failed job without help. It waits longer between each
attempt. When the retry budget is spent, the job stops in `dead`, and
`spoolctl retry` puts it back.

## Composition

A `ts` job can call `spoolctl add` to hand work to a durable queue. The tools
do not otherwise integrate.

## Limits and claims not to make

- Do not say task-spooler runs only one job at a time. `-S` sets the slot
  count.
- Do not say task-spooler has no ordering controls. It has `-u`, `-U`, and
  dependencies.
- Do not say task-spooler writes nothing on shutdown. `TS_SAVELIST` stores a
  dump on SIGTERM. It is not restored.
- Do not claim spoolctl allocates GPUs.

## Sources

- [GPU Task Spooler repository](https://github.com/justanhduc/task-spooler/tree/1522ab0fa22a3d45f425a3d1fab59774af031e7c)
- [task-spooler help text](https://github.com/justanhduc/task-spooler/blob/1522ab0fa22a3d45f425a3d1fab59774af031e7c/main.c#L484-L555)
- [task-spooler server SIGTERM handler](https://github.com/justanhduc/task-spooler/blob/1522ab0fa22a3d45f425a3d1fab59774af031e7c/server.c#L78-L97)
- [Original task-spooler by Lluís Batlle i Rossell](https://viric.name/soft/ts/)
- [spoolctl guarantees](https://github.com/Ozhiaki/spoolctl/blob/f95600bc9b3a93737f93c2e811b55b0129bb3cb6/docs/guarantees.md?plain=1)
