# Quickstart

Five minutes from zero to crash recovery. Every command below is runnable verbatim from a checkout.

## Set up a scratch project

```bash
QS=$(mktemp -d)
```

All commands use `--db $QS/queue.db` so nothing touches a real queue.

## Add jobs

```bash
python3 -m spoolctl add --db $QS/queue.db -- echo "hello from spoolctl"
python3 -m spoolctl add --db $QS/queue.db -- echo "second job"
python3 -m spoolctl add --db $QS/queue.db -- sleep 3
```

```
Added job 1
Added job 2
Added job 3
```

## Check status

```bash
python3 -m spoolctl status --db $QS/queue.db
```

```
canceled 0  dead 0  done 0  failed 0  queued 3  running 0
```

Three jobs queued, none running yet.

## Run a worker

```bash
python3 -m spoolctl work --db $QS/queue.db
```

The worker claims and executes jobs one at a time:

```
spoolctl: job 1 attempt 1 succeeded (0.0s) -> done
spoolctl: job 2 attempt 1 succeeded (0.0s) -> done
spoolctl: job 3 attempt 1 succeeded (3.0s) -> done
```

Stop it with Ctrl-C when the queue is empty.

## Read output

```bash
python3 -m spoolctl output --db $QS/queue.db 1
```

```
=== job 1 attempt 1 stdout ===
hello from spoolctl
=== job 1 attempt 1 stderr ===
```

stdout and stderr are captured per attempt and persist across retries.

## Crash recovery

This is what spoolctl is for. Add a long-running job and kill the worker mid-execution:

```bash
python3 -m spoolctl add --db $QS/queue.db -- sleep 120
python3 -m spoolctl work --db $QS/queue.db &
```

Wait a few seconds for the worker to claim the job, then check status:

```bash
python3 -m spoolctl status --db $QS/queue.db
```

```
canceled 0  dead 0  done 3  failed 0  queued 0  running 1
```

Now kill the worker with SIGKILL -- no cleanup possible:

```bash
kill -9 %1
```

The job still shows as `running` because no one has checked yet:

```bash
python3 -m spoolctl status --db $QS/queue.db
```

```
canceled 0  dead 0  done 3  failed 0  queued 0  running 1
```

Start a new worker. It will detect the dead worker via heartbeat staleness and PID liveness checks, reap the abandoned attempt, and re-queue the job:

```bash
python3 -m spoolctl work --db $QS/queue.db &
```

After roughly 30 seconds (the reap threshold -- see [Guarantees](guarantees.md) for why this delay exists and what it protects against), the new worker reaps and re-executes:

```
spoolctl: reaped job 4 (worker pid <PID> died); now queued
```

The job is back in the queue with its crash count incremented. The new worker claims and runs it again.

## Clean up

```bash
rm -rf $QS
```

## Next steps

- [Concepts](concepts.md) for the object model and state machine
- [Guarantees](guarantees.md) for the full reliability story
- [Agent Guide](agent-guide.md) for building scripts and agents on top of spoolctl
