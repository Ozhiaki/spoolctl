# Guarantees

spoolctl makes specific reliability claims, each backed by a mechanism, a test, and an honestly stated failure mode.

## Atomic claiming

**Claim:** Two live workers never own the same attempt at the same time.

**Mechanism:** `claim_next` opens a `BEGIN IMMEDIATE` transaction, selects the highest-priority eligible job, atomically updates it to `running` with the claiming worker's ID and PID, inserts the attempt row, and commits. `BEGIN IMMEDIATE` takes SQLite's write lock at transaction open, so concurrent claimants serialize rather than racing to upgrade a read lock.

**Test:** `tests/test_concurrency.py` runs multiple workers against the same queue and verifies no job is double-claimed.

**Failure mode:** SQLite's `BUSY` timeout (5 seconds, configurable) means a worker waiting for the write lock can time out under extreme contention. The job stays queued and is claimed on the next pass.

## Crash recovery

**Claim:** If a worker dies (SIGKILL, power loss, OOM), its in-flight job is eventually re-queued and re-executed.

**Mechanism:** Workers heartbeat into the database every 5 seconds (configurable). A running job whose heartbeat is older than the reap threshold (30 seconds by default, computed as `max(4 * heartbeat_interval, 30.0)`) becomes a reap candidate. Before reaping, the reaper confirms the worker PID is dead:

- `os.kill(pid, 0)` raises `ProcessLookupError` → dead.
- `os.kill(pid, 0)` raises `PermissionError` → inconclusive, treated as alive.
- `os.kill(pid, 0)` succeeds → process exists, but check `ps` for the `spoolctl` command marker. If the command line does not contain `spoolctl`, the PID has been recycled by an unrelated process and is treated as dead.

The reap itself runs under its own `BEGIN IMMEDIATE` and re-verifies staleness and ownership inside the transaction, so a false candidate that heartbeated between the candidate scan and the reap attempt is not touched.

**Test:** `tests/test_reaper.py` simulates worker death and verifies the full reap cycle: stale heartbeat detection, PID liveness confirmation, requeue with incremented crash count, and correct `failure_reason` recording.

**Failure mode:** A worker that is alive but hung (not heartbeating, not dead) cannot be distinguished from a dead worker after the reap threshold expires. spoolctl deliberately accepts this: "inconclusive means leave it alone" is the conservative default, but a truly hung process that stops heartbeating will eventually be reaped. The hung-but-alive worker is an accepted cost of not requiring a supervisor daemon.

## At-least-once execution

**Claim:** Every job that enters the queue is eventually executed at least once, unless it is explicitly canceled or exhausts its retry budget. A command can be re-executed after ambiguous worker death.

**Mechanism:** The crash recovery mechanism above re-queues jobs whose workers died. Retry backoff re-queues jobs that fail. The combination means a job is executed until it succeeds, is canceled, or reaches its dead-letter budget.

**What re-executes:** If a worker dies after the command finishes but before recording the result, the job is re-queued and the command runs again. The output from the previous attempt is preserved (the stdout/stderr files already exist), but the command itself has no transactional guarantee.

This is at-least-once, not exactly-once. For shell commands with side effects, the difference matters: a command that charges money or sends email can execute more than once after an ambiguous crash. Idempotency keys (`--key`) prevent duplicate *submission*, not duplicate *execution*.

**Test:** `tests/test_execute.py` covers the success/failure/timeout/crash paths and verifies correct state transitions and attempt recording.

**Failure mode:** There is no mechanism to prevent re-execution of a command whose side effects already completed. This is a fundamental limit of at-least-once semantics with arbitrary shell commands.

## Retry and backoff

**Claim:** Failed jobs are retried with exponential backoff up to a configurable budget. Job-owned failures and worker crashes have separate budgets.

**Mechanism:** `--max-retries` (default: 3) controls the job-owned failure budget (non-zero exit code, timeout, spawn failure). `--max-crashes` (default: unbounded) controls the worker-crash budget. Backoff is exponential: `min(60, 2 * 2^(attempts-1))` seconds, producing delays of 2, 4, 8, 16, 32, 60, 60, ... seconds.

When a job exhausts its retry budget, it transitions to `dead` and is not retried automatically. A dead job can be manually retried with `retry --force`.

**Test:** `tests/test_execute.py` verifies backoff timing and dead-letter transitions.

**Failure mode:** The backoff cap of 60 seconds is fixed. Long-running jobs that fail due to transient conditions (e.g., a downstream service outage lasting hours) exhaust their retry budget quickly.

## Per-job timeout with process-group kill

**Claim:** A job that exceeds its timeout is terminated, including all child processes.

**Mechanism:** Jobs are spawned with `start_new_session=True`, making the child a process-group leader. On timeout: SIGTERM the process group → wait up to 5 seconds → SIGKILL the process group → wait unconditionally. The group kill reaches grandchildren that the child forked.

**Test:** `tests/test_execute.py` verifies timeout enforcement and process-group cleanup.

**Failure mode:** A child process that detaches from its session (calls `setsid()` itself) escapes the group kill. This is rare for typical workloads and is not something spoolctl can prevent without a container or cgroup.

## Output durability

**Claim:** stdout and stderr are captured per attempt and persist across retries, crashes, and restarts.

**Mechanism:** Each attempt's stdout and stderr are written directly to files (no pipes or in-memory buffering). The file paths are recorded in the `attempts` table. Output from all attempts is available via `output`, not just the latest.

**Failure mode:** Output files live in the spool directory alongside the database. If the filesystem fills or the spool directory is deleted, output is lost. spoolctl does not replicate output to a remote store.

## Scope

spoolctl is a single-machine, local-filesystem tool. It relies on SQLite's file-level locking, which requires a local filesystem. NFS, CIFS, and other network filesystems do not provide the locking guarantees SQLite depends on.

All workers must see the same database file on the same local disk. There is no replication, no remote coordination, and no network surface.

## Test knobs

`SPOOLCTL_TEST_HEARTBEAT_INTERVAL` and `SPOOLCTL_TEST_REAP_THRESHOLD` are environment variables that override the heartbeat interval and reap threshold. They exist so the test suite can compress a 30-second reap cycle into a fast test. They are test knobs, not production tuning.

Both are published in `capabilities --json` because they are part of the observable contract. See [Limits Reference](reference/limits.md) for their bounds.
