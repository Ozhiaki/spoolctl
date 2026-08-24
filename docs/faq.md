---
title: FAQ
description: Read direct answers to common questions about spoolctl.
bucket: project
order: 30
---

# FAQ

**Is this exactly-once?**

No, and nothing running arbitrary shell commands can be. At-least-once with absolute mutual exclusion (never two live workers on one job) is the honest maximum, and it's what spoolctl guarantees. See [Guarantees](/docs/guarantees/).

**Why not just use pueue?**

If you're a human watching your queue, do. pueue's daemon is also its weakness for unattended use: it's a single coordinator with no automatic retry, no backoff, and no dead-letter state. spoolctl is for work that has to survive nobody watching. See [Comparison](/docs/comparison/).

**Why not Celery / RQ / huey?**

Those queue *functions in your application*. spoolctl queues *commands on your machine*. If you have an app with a worker fleet and a broker, you don't need spoolctl.

**Why not cron or systemd timers?**

cron and systemd timers are schedulers, not queues. They answer "run this at 2 AM" but have no concept of job state, retry on failure, concurrent claiming, or crash recovery. A common pattern is using cron to submit jobs to spoolctl. See [Comparison](/docs/comparison/).

**What does "agent-native" mean concretely?**

The assumed operator is a process that runs concurrently with others it doesn't know about, gets SIGKILLed routinely, and needs a successor -- possibly a different process entirely -- to find the work, its state, and its output. Every guarantee in the design exists to serve that operator. Humans are welcome too.

**Why SQLite instead of lockfiles or a spool directory?**

Atomic claim-one-of-N under concurrency is exactly what a transactional database does and exactly what flock choreography does badly. WAL mode makes readers free, and the whole queue is one inspectable file.

**Why Python stdlib only?**

Zero-dependency single-file Python is the most installable software artifact that exists: every macOS and Linux box can run it, and an agent can "install" it by writing a file.

**Can two projects share one database?**

They can, but should not. Use named queues (`--queue`) to partition work within a single project. Separate projects should use separate database files to avoid unintended interactions and to make cleanup (`prune`) safe.

**What happens if two workers use different spoolctl versions?**

Migrations run automatically on database open, so the first worker to open a database at a newer schema version upgrades it. The older worker will then see a `SchemaTooNewError` and refuse to operate. All workers against the same database should run the same spoolctl version.

**Is the database file safe to copy or back up?**

Yes, with the standard SQLite WAL caveat: copy both `queue.db` and `queue.db-wal` (if it exists) together, or use `sqlite3 queue.db '.backup backup.db'` for a consistent snapshot. A copy of `queue.db` alone while a WAL file exists may be incomplete.

**What happens if the disk fills mid-job?**

The job's stdout/stderr capture may be truncated. The heartbeat update or result recording may fail with an SQLite error. If the worker cannot record the result, the job stays `running` in the database and will be reaped by the next worker after the disk issue is resolved. No data corruption occurs; SQLite's write-ahead log ensures atomicity.
