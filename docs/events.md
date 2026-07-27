# Events

The `job_events` table is an append-only ledger of every state transition. Subscribers are readers of the SQLite file; there is no daemon, no broker, and no push mechanism.

## One-shot query

```bash
python3 -m spoolctl events --db ./queue.db --json
```

Returns an envelope with `data.events` (array of event objects) and `meta.pagination.cursor` (the resume point for the next query).

Flags:

| Flag | Default | Description |
|---|---|---|
| `--since-id N` | 0 | Return events after this event ID. Also aliased as `--since-cursor`. |
| `--limit N` | 1000 | Maximum events to return. `0` means unlimited. |
| `--job N` | all | Filter to events for a specific job. |

## Long-poll (`--wait`)

```bash
python3 -m spoolctl events --db ./queue.db --json --wait
```

If no events are available, the command blocks until at least one event arrives or the wait timeout expires.

| Flag | Default | Description |
|---|---|---|
| `--wait` | off | Enable long-polling. |
| `--wait-timeout N` | 30.0 | Maximum seconds to wait for events. |

The response envelope includes `meta.wait.reason`: either `records_available` (events arrived) or `timeout` (the wait expired without new events).

## Follow mode (`--follow --json`)

```bash
python3 -m spoolctl events --db ./queue.db --follow --json
```

Streams events as NDJSON (one JSON object per line). The command runs until interrupted, a limit is reached, or an error occurs.

Each line is either a **data frame** (an event object) or a **control frame**.

### Control frames

Control frames have the shape `{"control": {"type": "<type>", "reason": "<reason>", ...}}`:

| Type | Reason | Meaning |
|---|---|---|
| `end` | `idle_timeout` | No events arrived within `--idle-timeout` seconds. |
| `end` | `max_events` | The `--max-events` limit was reached. |
| `error` | `sqlite_error` | A database error occurred. Includes `message` and `exit_code`. |

### Follow-mode flags

| Flag | Default | Description |
|---|---|---|
| `--follow` | off | Enable streaming mode. |
| `--poll-interval N` | 0.5 | Seconds between polls. |
| `--max-events N` | unlimited | Stop after this many events. |
| `--idle-timeout N` | unlimited | Stop after this many seconds with no new events. |
| `--since-id N` | current high-water | Start from this event ID. |

`--follow` and `--wait` are mutually exclusive. `--limit` is not valid with `--follow` (use `--max-events` instead).

## Consuming events programmatically

### Cursor-based pagination

```bash
# First page
python3 -m spoolctl events --db ./queue.db --json --limit 100

# Next page (use cursor from meta.pagination.cursor)
python3 -m spoolctl events --db ./queue.db --json --limit 100 --since-id 42
```

The cursor is the event ID to resume from. It is stable across database changes.

### Follow mode for agents

For agents that need real-time event processing:

```bash
python3 -m spoolctl events --db ./queue.db --follow --json --idle-timeout 60
```

Parse each line as JSON. Data frames have event fields (`id`, `job_id`, `event`, `at`, `worker_id`, `detail`). Control frames have `control.type` and `control.reason`. An `end` control frame means the stream terminated normally; an `error` control frame means it terminated due to a problem.

## Human output

Without `--json`, events are printed in a human-readable table format. `--follow` without `--json` prints one line per event as it arrives.
