---
title: Execution
description: Learn how spoolctl executes commands, timeouts, and process groups.
bucket: guides
order: 40
---

# Execution

## Command form

### argv vector (default)

```bash
python3 -m spoolctl add -- echo "hello world"
```

Everything after `--` is stored as an argv vector and executed with `shell=False`. The command runs as a direct `exec`, not through a shell. This is the safer default: no shell expansion, no injection surface.

### Shell string (`-c`)

```bash
python3 -m spoolctl add -c 'echo $HOME && date'
```

`-c STRING` is translated at submit time into `["sh", "-c", STRING]`. The command runs through `/bin/sh`. Shell features (pipes, redirection, variable expansion) work, but the injection risks are the shell's. The stored argv is always `["sh", "-c", ...]` — there is no `subprocess(shell=True)` at runtime.

`--` argv and `-c` are mutually exclusive.

## Working directory

`--cwd <path>` sets the working directory for the command. The path is resolved at submit time:

- Relative paths are resolved against the current directory of the `add` command.
- `os.path.abspath` is applied but symlinks are not collapsed (`os.path.realpath` is deliberately not used).

If the directory does not exist or is not a directory at execution time, the attempt fails with `spawn_failed` and is governed by `--max-retries`.

When `--cwd` is not specified, the command inherits the worker's current directory.

## Environment variables

`--env KEY=VALUE` passes environment variables to the command. The flag is repeatable:

```bash
python3 -m spoolctl add --env MODEL=large --env GPU=0 -- python train.py
```

Semantics:

- Keys and values are split on the first `=`. An empty value (`--env KEY=`) is valid.
- Repeated keys are last-wins: `--env X=1 --env X=2` stores `X=2`.
- At runtime, job env vars are layered over `os.environ`: they override matching keys but do not replace the entire environment.
- Key length: max 128 characters. Value length: max 4096 characters.
- NUL bytes are rejected in both keys and values.
- `add` and `list` expose env key *names* only. `show` is the explicit plaintext surface that displays values. This is a deliberate privacy boundary: listing a queue does not reveal secret values, but anyone with `show` access sees them.

Env values are stored in the database in plaintext. Do not pass secrets via `--env`; use the process environment or a secret store instead. See [Security](/docs/security/).

## Timeouts

`--timeout <seconds>` sets a per-job timeout (default: 300 seconds, range: 1 to 2^63-1).

When a job exceeds its timeout:

1. SIGTERM is sent to the job's process group (including any child processes).
2. The worker waits up to 5 seconds for graceful shutdown.
3. If still alive, SIGKILL is sent to the process group.
4. The attempt is recorded as `timed_out` with failure reason `timeout`.

The process-group kill is made possible by `start_new_session=True` on the child process. See [Guarantees](/docs/guarantees/) for the limitation when a child calls `setsid()`.

## Retry budgets

Two independent budgets control how many times a job is re-executed:

### `--max-retries` (default: 3)

Governs job-owned failures: non-zero exit code, timeout, spawn failure. After `max_retries` consecutive job-owned failures, the job transitions to `dead`.

### `--max-crashes` (default: unbounded)

Governs worker crashes. Each time a worker dies and the job is reaped, the crash count increments. If `max_crashes` is set and the crash count exceeds it, the job transitions to `dead`.

The two budgets are independent. A job with `--max-retries 3 --max-crashes 2` can fail up to 3 times from its own errors and be reaped up to 2 times from worker crashes, for a total of up to 8 execution attempts.

Backoff applies to both paths: `min(60, 2 * 2^(attempts-1))` seconds. See [Guarantees](/docs/guarantees/) for the full formula.
