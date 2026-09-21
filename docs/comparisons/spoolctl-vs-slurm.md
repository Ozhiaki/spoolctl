---
title: spoolctl and Slurm
description: Compare spoolctl with Slurm, the HPC cluster workload manager. Slurm has a durable queue, dependencies, arrays, and requeue; spoolctl gives the same crash safety from one SQLite file with no daemon and no root.
bucket: concepts
order: 290
---

# spoolctl and Slurm

**Research date:** 2026-09-21
**spoolctl source baseline:** `main`, commit [`f95600b`](https://github.com/Ozhiaki/spoolctl/tree/f95600bc9b3a93737f93c2e811b55b0129bb3cb6)
**Slurm source baseline:** `master`, commit [`5d58f68`](https://github.com/SchedMD/slurm/tree/5d58f686bf075c9eae7cb313c5470200edbbc335) (Slurm 26.11 man pages)

## Conclusion

Slurm is the workload manager on most HPC clusters. It has a durable priority
queue, job dependencies, job arrays, per-job time limits, and automatic requeue
after a node failure. On queue features it overlaps with spoolctl more than any
other tool here. People ask for "Slurm for one machine," so the comparison is
fair to make.

The difference is weight, not features. Slurm is a cluster system. To run it you
stand up a central control daemon (`slurmctld`), a node daemon (`slurmd`) on each
compute host, a shared authentication service (usually MUNGE), and a
`slurm.conf` file, with an optional accounting database on top. That is the right
cost for a shared cluster of many nodes and users. It is a large cost to run one
machine's jobs.

Use **Slurm** when you schedule a real cluster: many nodes, many users, resource
accounting, fair-share priority. Use **spoolctl** when you want a durable,
crash-safe queue on one machine from a single SQLite file, with no daemon, no
root, no auth service, no config, and macOS support.

## Scope and architecture

Slurm has a central management daemon, `slurmctld`. It accepts jobs, holds the
queue, and allocates resources. It writes state to a `StateSaveLocation`
directory and recovers running and queued jobs from that checkpoint when it
restarts, so a controller restart does not lose the queue. A backup controller
can take over if the primary fails. Each compute node runs a `slurmd` daemon that
launches and monitors the job there.

So Slurm does have a coordinator, and it is durable. The trade is that the
coordinator is a service you deploy and keep running, with its own auth and
config, not a file you open.

spoolctl has no coordinator. Jobs are rows in one SQLite file. Any number of peer
workers claim jobs, each in one transaction, and write a heartbeat while a job
runs. A job is reclaimed only after its owner process is confirmed dead. See
[Guarantees](/docs/guarantees/).

## Capability comparison

| Area | Slurm at `5d58f68` | spoolctl at `f95600b` |
| --- | --- | --- |
| Primary job | Schedule batch jobs across a cluster of nodes. | Run commands that must survive the death of any runner. |
| Coordinator | `slurmctld` control daemon, plus `slurmd` per node. | None. One SQLite file. |
| Queue state survives coordinator death | Yes. Recovered from `StateSaveLocation` on restart; optional backup controller. | Yes. Durable in SQLite. |
| Running job survives runner death | Yes, if requeued. On node failure the batch script is requeued and restarted from its beginning. | Yes. Re-run after the owner is confirmed dead. |
| Never run twice at once | The controller owns allocation; it does not assign one job to two nodes. | Enforced. One claim per attempt in one transaction. |
| Automatic retry | Requeue on node failure or preemption by default; `--no-requeue` to disable, `--requeue` to force. | Built in, with a budget. |
| Retry backoff | Not a per-job backoff; requeued jobs return to the queue and reschedule. | Exponential by default. |
| Failed state | Job states: `FAILED`, `NODE_FAIL`, `TIMEOUT`, etc. | `max-retries`, then `dead`. |
| Per-job timeout | Yes: `--time`. At the limit each task gets SIGTERM then SIGKILL after `KillWait`. | Yes. Default 300 s. Kills the process group. |
| Dependencies | Yes: `--dependency=after/afterok/afternotok/singleton`. | No. |
| Job arrays | Yes: `--array`, with a running-task cap. | No. Submit N jobs. |
| Resource scheduling | Yes: CPUs, memory, GPUs/GRES, partitions, QOS, fair-share priority. | No. A slot count per lane. |
| Output | Files, `--output`/`--error` (default `slurm-%j.out`). | Files per attempt; `output` verb from any process. |
| Machine interface | `squeue --json`/`--yaml`, `scontrol show --json`, `slurmrestd`. | JSON envelope on every verb, stable error codes, schemas. |
| Install | `slurmctld` + `slurmd` daemons, an auth service (MUNGE), `slurm.conf`; optional accounting DB. Root. | Python 3 standard library only. No root. |
| Platforms | Linux. | macOS, Linux. |

## What Slurm does well

- It is a durable, recoverable queue. `slurmctld` checkpoints state and restores
  running and queued jobs on restart.
- It requeues a job after a node fails, and restarts the batch script, so work is
  not lost to hardware loss.
- It has the scheduling features a shared cluster needs: dependencies, job
  arrays, per-job time limits, and resource-aware priority across CPUs, memory,
  and GPUs.
- It scales to many nodes and many users, which spoolctl does not attempt.
- It exposes machine-readable state through `squeue --json` and a REST daemon.

## What spoolctl does differently

- spoolctl is one file, not a set of daemons. There is no control daemon, no node
  daemon, no auth service, and no config file to deploy or keep alive.
- spoolctl needs no root and no shared secret. It runs as you, coordinating
  through a SQLite file you can read.
- spoolctl reclaims a job only after the owner process is confirmed dead, from
  peer workers with no central controller. Slurm's safety comes from the
  controller instead.
- spoolctl retries with exponential backoff by default. Slurm requeues to the
  scheduler; backoff is not its model.
- spoolctl runs on macOS. Slurm is a Linux cluster system.

Slurm is heavier because it does more: it schedules a cluster. spoolctl does one
of those jobs, crash-safe local execution, at the cost of one SQLite file.

## Composition

They compose in one direction only, and rarely need to. Inside a Slurm batch
script you could run a spoolctl worker to drain a local queue on the allocated
node. But if you have Slurm, you already have its queue. spoolctl is for the
machine that does not run a cluster manager.

## Limits and claims not to make

- Do not say Slurm loses its queue when the controller restarts. It recovers from
  `StateSaveLocation`, and a backup controller can take over.
- Do not say Slurm cannot recover a running job. On node failure it requeues the
  job and restarts the batch script from the beginning (unless `--no-requeue`).
- Do not say Slurm lacks retry, timeout, dependencies, or arrays. It has all of
  them.
- Do not frame the difference as features. Slurm matches or exceeds spoolctl on
  queue features. The difference is deployment weight and platform.
- Do not compare on macOS. Slurm is a Linux cluster system.
- Do not call spoolctl a scheduler. It has no resource-aware or fair-share
  scheduling; it drains a queue under a slot count.

## Sources

- [`slurmctld(8)`](https://github.com/SchedMD/slurm/blob/5d58f686bf075c9eae7cb313c5470200edbbc335/doc/man/man8/slurmctld.8#L7-L13) — central management daemon; state recovery from the last checkpoint (`StateSaveLocation`); backup server.
- [`sbatch(1)` `--time`](https://github.com/SchedMD/slurm/blob/5d58f686bf075c9eae7cb313c5470200edbbc335/doc/man/man1/sbatch.1#L2508-L2517) — total run-time limit; SIGTERM then SIGKILL after `KillWait`. Requeue: [`--no-requeue`](https://github.com/SchedMD/slurm/blob/5d58f686bf075c9eae7cb313c5470200edbbc335/doc/man/man1/sbatch.1#L1899-L1905) — default requeue on node failure or preemption; the batch script restarts from its beginning. Also [`--dependency`](https://github.com/SchedMD/slurm/blob/5d58f686bf075c9eae7cb313c5470200edbbc335/doc/man/man1/sbatch.1#L645-L648) and [`--array`](https://github.com/SchedMD/slurm/blob/5d58f686bf075c9eae7cb313c5470200edbbc335/doc/man/man1/sbatch.1#L135-L145).
- [`squeue(1)` `--json`/`--yaml`](https://github.com/SchedMD/slurm/blob/5d58f686bf075c9eae7cb313c5470200edbbc335/doc/man/man1/squeue.1#L1392-L1393) — machine-readable queue state.
- [spoolctl guarantees](https://github.com/Ozhiaki/spoolctl/blob/f95600bc9b3a93737f93c2e811b55b0129bb3cb6/docs/guarantees.md?plain=1)
