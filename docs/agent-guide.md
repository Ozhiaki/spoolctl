---
title: Agent Guide
description: Use spoolctl safely from automated and agent-driven workflows.
bucket: guides
order: 80
---

# Agent Guide

This page is for agents, scripts, and automated consumers that integrate with spoolctl programmatically.

## Discover the contract

spoolctl publishes its full contract through four discovery verbs:

| Verb | Purpose |
|---|---|
| `capabilities --json` | Every verb, flag, exit code, error code, limit, and semantic rule. |
| `schema --json` | The JSON envelope schema and per-verb data schemas. |
| `brief` | A compact natural-language summary (budget: 700 tokens). |
| `robot-docs guide --json` | Structured agent-facing documentation. |

These are the contract's public surface. Any behavior not documented in them is an implementation detail and may change.

## Check readiness

Before operating on a queue, run [doctor](/docs/doctor/):

```bash
python3 -m spoolctl doctor --db ./queue.db --json
```

Key off `data.ready`, not `errors`. See [Doctor](/docs/doctor/) for the full exit contract.

## Submit-many-then-wait

The core parallelism primitive: submit a batch of jobs, then wait for all of them to finish.

```bash
python3 -m spoolctl add --db ./queue.db --json --key task-1 -- command-1
python3 -m spoolctl add --db ./queue.db --json --key task-2 -- command-2
python3 -m spoolctl add --db ./queue.db --json --key task-3 -- command-3
python3 -m spoolctl wait --db ./queue.db --json 1 2 3
```

The `wait` verb blocks until all listed jobs reach a terminal state (`done`, `dead`, or `canceled`). A job with retry budget left sits in `queued` between attempts, which is not terminal.

### Exit code 6 and `data.all_succeeded`

`wait` exits 0 if every job ended `done`. If any job ended `dead` or `canceled`, wait exits 6.

Exit 6 is the one deliberate exception in spoolctl's exit contract: it pairs with `ok:true` and empty `errors[]`. The tool call succeeded; the exit code carries the job outcome for shell scripts.

**Envelope consumers** should key off `data.all_succeeded`, not the exit code.

## One-call verdict: `feedback`

After a job settles, an agent usually needs four things: did it finish, did it succeed, why not, and what to do next. `feedback` answers all four in one call.

```bash
python3 -m spoolctl feedback --db ./queue.db --json 1
```

```json
{
  "job_id": 1,
  "state": "dead",
  "terminal": true,
  "succeeded": false,
  "exit_code": 3,
  "failure_reason": "process_exit",
  "last_error": "exit 3",
  "attempts": 4,
  "attempts_total": 4,
  "latest_attempt_no": 4,
  "duration_seconds": 0.014,
  "remediation": "spoolctl output 1",
  "streams": {
    "stdout": {"tail": "hi\n", "size_bytes": 3, "truncated": false, "missing": false, "path": "..."},
    "stderr": {"tail": "bad\n", "size_bytes": 4, "truncated": false, "missing": false, "path": "..."}
  }
}
```

Read `terminal` first, then `succeeded`. `succeeded` is tri-state: `true`, `false`, or `null` for every non-terminal state. Never parse the prose.

The counting fields are three different numbers and none of them is derivable from the others:

- `attempts` -- the job's live retry budget counter, which a manual `retry` resets to 0.
- `attempts_total` -- every attempt row ever recorded for the job, which nothing resets.
- `latest_attempt_no` -- the attempt the streams come from, or `null` when the job has never been claimed.

`streams` always carries both `stdout` and `stderr`. Three distinct situations are distinguishable: `path: null` means no attempt has run; `missing: true` with a non-null path means the file was deleted or unreadable; `size_bytes: 0` with `missing: false` means the attempt genuinely produced nothing.

`--tail-bytes N` (1..65536, default 2048) widens each tail; `truncated` says whether bytes were dropped from the front. `--stream {stdout,stderr,both}` narrows the human-readable rendering only -- the JSON payload always carries both streams.

`remediation` names the next command to run: `spoolctl output <id>` for a dead job, `spoolctl wait <id>` for a running one, `spoolctl work --drain` for a job nothing has picked up yet.

A job that has never been attempted also returns a `NO_ATTEMPTS_YET` warning in the envelope.

## Idempotency keys

The `--key` flag on `add` prevents duplicate submission of the same logical task:

```bash
python3 -m spoolctl add --db ./queue.db --json --key daily-report -- generate-report
```

If an active job (state `queued` or `running`) already exists with the same key, the new `add` deduplicates instead of creating a second job:

- **Execution payload matches** (argv, timeout, retries, priority, queue, cwd, env, max-crashes): the existing job is returned with `data.deduplicated: true`. No new job is created.
- **Execution payload differs**: `IDEMPOTENCY_CONFLICT` error, exit 5. The conflict is deliberate: the same key should not point at two different commands.
- **Metadata differs** (tags, note, schedule): the existing job is returned with a `IDEMPOTENCY_METADATA_DIFFERS` warning. Metadata differences are tolerated, not rejected.

Idempotency keys solve the crashing-*submitter* problem: if the agent dies after submitting but before recording the submission, re-running the same `add --key` is safe.

When `data.deduplicated` is true, `data.idempotency` contains `key`, `metadata_differs`, and `metadata_differences`.

## Tags and notes for cross-session handoff

Tags (`--tag key=value`, repeatable) and notes (`--note "text"`) are metadata stored with the job. They are visible in `list` and `show` output.

A worked recipe for cross-session agent handoff:

1. Agent A submits jobs with `--tag owner=agent-a --tag session=abc123`.
2. Agent A dies.
3. Agent B starts, queries `list --tag owner=agent-a --json`, and discovers its predecessor's work.
4. Agent B uses `wait` on the discovered job IDs, reads their output, and continues.

Tags are key-value pairs with length limits (key: 64 chars, value: 256 chars, max 16 tags per job).

## Safety gates

Some operations require explicit confirmation:

| Verb | Gate | Override |
|---|---|---|
| `prune` | Destructive: permanently deletes jobs and their output. | `--yes` to confirm, `--dry-run` to preview. |
| `cancel --running` | Interrupting: kills a running job's process group. | `--yes` to confirm. |
| `retry` on running job | Interrupting: force-retries a job that is currently executing. | `--force` to confirm. |

Without the confirmation flag, these operations return `SAFETY_BLOCK` (exit 2).

## The `commands[]` field

Every envelope includes a `commands` array. In the current contract, `commands[]` is always empty. It is a reserved contract slot for future use. Entries in `commands[]` are candidates, not instructions: an agent may inspect and choose to follow them, but is never obligated to execute them.

## Consuming `schema --json`

```bash
python3 -m spoolctl schema --json
```

The schema envelope contains:

- `data.envelope_schema`: the JSON schema for the envelope structure itself.
- `data.verbs`: per-verb data schemas.
- `data.streams`: schemas for streaming output (events follow mode).

Use the schema to validate envelope payloads programmatically.
