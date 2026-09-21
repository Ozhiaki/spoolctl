---
title: spoolctl and pueue
description: Compare spoolctl crash recovery and retries with pueue, the daemon-based interactive command queue.
bucket: concepts
order: 210
---

# spoolctl and pueue

**Research date:** 2026-09-21
**spoolctl source baseline:** `main`, commit [`f95600b`](https://github.com/Ozhiaki/spoolctl/tree/f95600bc9b3a93737f93c2e811b55b0129bb3cb6)
**pueue source baseline:** v4.0.4 (released 2026-03-02); `main`, commit [`193ed22`](https://github.com/Nukesor/pueue/tree/193ed2264338bd30a06e347b48183a8800bd178b)

## Conclusion

Use [**pueue**](https://github.com/Nukesor/pueue) when a person manages
long-running commands and wants to see and steer them. pueue can pause and
resume tasks, send input to a running task, reorder the queue, set task
dependencies, and run on Windows. spoolctl does none of these.

Use **spoolctl** when no person watches the queue. If a pueue task is running
when the daemon crashes, pueue marks it `Killed` on restart and does not run it
again. If a spoolctl worker dies, another worker confirms that the owner
process is gone and runs the job again. spoolctl also retries failed jobs with
backoff. pueue retries only when a person runs `pueue restart`.

## Scope and architecture

pueue has a client, `pueue`, and a daemon, `pueued`. The daemon owns all task
processes. Clients talk to it over a socket. The daemon writes its state to
`state.json` after each change. It writes a temporary file first and renames
it, so a crash during a write does not corrupt the state. See the pinned
[state save and restore code](https://github.com/Nukesor/pueue/blob/193ed2264338bd30a06e347b48183a8800bd178b/pueue/src/daemon/internal_state/state.rs#L243-L381).

On restart, the daemon reads `state.json`. It changes every task that was
`Running` or `Paused` to `Done` with result `Killed`. It pauses every group
that has queued tasks, "to prevent unwanted execution of tasks due to a system
crash." A person must start those groups again.

spoolctl has no daemon. Each `spoolctl work` process is a peer. All workers
coordinate through one SQLite file. A worker claims a job in one
`BEGIN IMMEDIATE` transaction and writes a heartbeat while the job runs. See
[Guarantees](/docs/guarantees/).

## Capability comparison

| Area | pueue v4.0.4 | spoolctl at `f95600b` |
| --- | --- | --- |
| Primary job | Manage long-running commands for a person. | Run commands that must survive the death of any runner. |
| Coordinator | The `pueued` daemon. | None. The SQLite file. |
| Queue state after a crash | Restored from `state.json`. | Durable in SQLite. |
| Running task after a crash | Marked `Killed`. Not run again. | Reclaimed and run again after the owner is confirmed dead. |
| Queued tasks after a crash | Group paused until a person resumes it. | Claimed by the next worker. |
| Retry of a failed task | Manual: `pueue restart`. | Automatic, exponential backoff, default budget 3, then `dead`. |
| Per-task timeout | Not in the reviewed source. | Yes. Default 300 s. Kills the process group. |
| Parallelism | Groups, each with a parallel-task limit. | Lanes (`--queue`), each with an optional fleet-wide `--slots` ceiling. |
| Dependencies | Yes. | No. |
| Delayed start | Yes. | Yes: `--after`, `--at`. |
| Priorities | `add --priority`, plus manual reorder with `switch`. | `--priority` at submit time. |
| Interaction | Pause, resume, send stdin, edit, reorder. | Cancel, retry, prune. No stdin, no pause. |
| Machine interface | JSON for `status` and `log`. | JSON envelope on every verb, stable error codes, schemas, capabilities. |
| Install | Rust binaries. | Python 3 standard library only. |
| Platforms | Linux, macOS, Windows. | macOS, Linux. |

## What pueue does well

- It is a mature, polished tool for people. Its status view, log view, and
  process controls are richer than anything in spoolctl.
- It persists queue state safely. The temp-file-then-rename write means a
  crash during a save does not leave a broken state file.
- Pausing groups after a crash is a careful choice for its audience. A person
  can check what happened before anything runs.
- It supports task dependencies and Windows. spoolctl supports neither.

The pinned [README feature list](https://github.com/Nukesor/pueue/blob/193ed2264338bd30a06e347b48183a8800bd178b/README.md?plain=1#L28-L60) documents these features.

## What spoolctl does differently

pueue's crash behavior is correct for a person at a keyboard. It is wrong for
an unattended operator. After a crash, pueue waits for a person. spoolctl
continues without one.

spoolctl never trusts a stale heartbeat alone. It reclaims a job only after it
confirms that the owner process is dead. If the check is inconclusive, it
leaves the job alone. Recovery can be slow, but two workers never run the same
attempt at the same time.

spoolctl keeps output from each attempt. After three retries, you can read all
three outputs, from any process.

## Composition

The tools do not integrate. They can share a machine. Use pueue for commands
you watch. Use spoolctl for commands that agents or scripts submit and nobody
watches.

## Limits and claims not to make

- Do not say pueue loses its queue when the daemon dies. It restores
  `state.json`.
- Do not say pueue has no failure state. Tasks end as `Success`, `Failed`,
  `Killed`, and other results.
- Do not call spoolctl exactly-once. A job can run again after an ambiguous
  crash.
- Do not say spoolctl replaces pueue for interactive work. It has no pause,
  no stdin, and no dependencies.

## Sources

- [pueue v4.0.4 release](https://github.com/Nukesor/pueue/releases/tag/v4.0.4), 2026-03-02
- [pueue README features and similar projects](https://github.com/Nukesor/pueue/blob/193ed2264338bd30a06e347b48183a8800bd178b/README.md?plain=1)
- [pueue state save and restore](https://github.com/Nukesor/pueue/blob/193ed2264338bd30a06e347b48183a8800bd178b/pueue/src/daemon/internal_state/state.rs#L243-L381)
- [spoolctl guarantees](https://github.com/Ozhiaki/spoolctl/blob/f95600bc9b3a93737f93c2e811b55b0129bb3cb6/docs/guarantees.md?plain=1)
