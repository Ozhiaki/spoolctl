# Changelog

All notable user-visible changes to spoolctl are documented here.

> **Status: pre-release.** The CLI surface may still move before a public
> package release, but the documented interface and guarantees are tested in
> this repository.

## v0.4.9 — Changelog and README Housekeeping

Housekeeping release. Zero runtime behavior change. `CONTRACT_VERSION` remains
`2`, `SCHEMA_VERSION` remains `6`.

- Added curated public `CHANGELOG.md` covering v0.1.0 through v0.4.9.
- Added `docs/changelog.md` symlink so the changelog renders in the mkdocs
  documentation site under Project.
- Slimmed `README.md` from ~330 to ~240 lines: removed version-specific
  sections, compressed redundant FAQ entries and detail blocks, added docs
  cross-references.
- Added changelog content gates to the test suite: version-ahead check and
  forward-language scan.

## v0.4.8 — Documentation Section

Docs-only release. Zero runtime behavior change.

- Added 19-page documentation tree under `docs/`: install,
  quickstart, concepts, guarantees, architecture, scheduling, execution, config,
  doctor, events, agent guide, JSON contract, comparison, landscape, FAQ,
  security, lineage, and project overview.
- Added a stdlib-only generator (`scripts/build_docs.py`) producing four
  reference pages (verbs, errors, limits, states) from the live contract.
  `--check` mode gates CI against drift.
- Added `mkdocs.yml` with mkdocs-material theme. `mkdocs-material` is an
  optional `[docs]` extra; `pip install spoolctl` remains stdlib-only.
- Added a Documentation section to the README linking into `docs/`.

## v0.4.7 — MCP Readiness Prep

Additive readiness release. `CONTRACT_VERSION` remains `2`, `SCHEMA_VERSION`
remains `6`.

- Added project-local config resolution. Database path precedence is now
  explicit: `--db` > `SPOOLCTL_DB` > `.spoolctl/config.json` `db_path` >
  `./.spoolctl/queue.db` relative to the project directory.
- Added `config-show`: reports the effective config path, config validity, DB
  path, DB source, and precedence without opening or creating the database.
- Added `config-validate [PATH]`: validates optional project JSON config
  without opening or creating the database. Missing config is a valid optional
  state.
- Added narrow `doctor`: bounded readiness diagnostic
  checking config validity, DB path resolution, spool-directory writability,
  database existence, SQLite read/write open, schema version, and contract
  metadata. Readiness failures exit `3` with envelope `ok:true`, `errors:[]`,
  and `data.ready:false`.
- Added contract completeness gates for verb tables, schema data, signature
  baselines, generated probes, and module-boundary coverage.

## v0.4.6 — Operation-Layer Refactor

No-feature refactor release preparing the codebase for MCP server mode.
`CONTRACT_VERSION` remains `2`, `SCHEMA_VERSION` remains `6`.

- Extracted a reusable operation layer: `status`, `output`, `add`, and `wait`
  now have typed operation inputs and direct tests independent of argparse.
- Extracted focused modules: `errors.py`, `models.py`, `validation.py`,
  `operations.py`, and `contract.py`. Static tests enforce module boundaries.
- `cli.py` remains the executable adapter: parser construction, envelope
  emission, rendering, stdout/stderr, and exit-code mapping.

## v0.4.5 — Contract Conformance

Hardened the CLI contract. `CONTRACT_VERSION` bumped to `2`. No v1
compatibility shim is planned before public release.

- Expanded `capabilities --json` into the probe source of truth: verbs, flags,
  positionals, malformed-input expectations, output modes, safety gates,
  idempotency behavior, schemas, code registry, environment variables, limits,
  scheduling, and execution semantics.
- Hardened parser totality: disabled flag abbreviation, rejected inert flags,
  made bare invocation an explicit error, diagnosed `add` command-tail
  ambiguity, and converted numeric/path/env/duration/timestamp/enum failures
  into structured contract errors.
- Added destructive gates: `prune` requires `--yes` unless `--dry-run`,
  `cancel --running` requires `--yes`, `retry --force` for running-job
  recovery.
- Added `IDEMPOTENCY_CONFLICT` for active-key execution mismatch and
  `IDEMPOTENCY_METADATA_DIFFERS` for metadata-only dedupe warnings.
- Declared and tested output modes: envelope, frames (`events --follow --json`),
  raw (`output --raw`), and human text.
- Added `robot-docs guide` with JSON output and updated top-level help, brief
  text, and schemas.

Compatibility: fresh databases still use schema version `6`.
`CONTRACT_VERSION` `2` removes accepted-but-unsafe or accepted-but-inert CLI
behaviors.

## v0.4.2 — Failure Reason Enum

Made failures machine-classifiable. `CONTRACT_VERSION` remains `1`.

- Added durable per-attempt `failure_reason` with a stable enum:
  `process_exit`, `timeout`, `spawn_failed`, `worker_crash`, `canceled`, and
  `unknown`.
- Added `job.last_failure_reason` to `show --json`, derived from attempts using
  current-outcome semantics. Recovered jobs report `null`.
- Updated `capabilities --json` to publish the `failure_reasons` registry and
  `schema --json` to validate nullable failure-reason fields.

Compatibility: schema version `6`. Existing databases migrate forward. Migration
backfills obvious legacy states but maps ambiguous historical `failed` rows to
`unknown`.

## v0.4.1 — Execution Fidelity

Completed the run primitive. `CONTRACT_VERSION` remains `1`.

- Added per-job working directory with `add --cwd DIR`. Resolved to an absolute
  path at submit time without symlink collapse; missing cwd at runtime is a
  normal spawn failure.
- Added per-job environment overrides with repeatable `add --env K=V`. Overrides
  augment the worker environment, allow empty values, and use last-wins
  semantics. `add`/`list` expose env key names only; `show` is the explicit
  plaintext surface.
- Split worker-crash accounting from job-owned failure retry accounting.
  `--max-retries` governs nonzero exits, timeouts, and spawn failures.
  `--max-crashes N` bounds crash redelivery (default unbounded).

Compatibility: schema version `5`. Existing databases migrate forward. Migration
backfills `crashes` from canonical abandoned attempts.

## v0.4.0 — Scheduling-Lite and Lanes

Added scheduling and resource isolation. `CONTRACT_VERSION` remains `1`.

- Added delayed submission: `add --after DURATION` and `add --at TIMESTAMP`.
  Delayed jobs stay `queued` with a future `next_run_at`; there is no new state.
- Added submit-time priorities with `add --priority N`. Claim order is now
  `priority DESC, next_run_at ASC, id ASC`.
- Added named queues with `add --queue NAME` and `work --queue NAME`. Default
  workers serve only the `default` lane.
- Added per-lane slot ceilings with `work --slots N`, enforced inside the
  SQLite claim transaction.
- Updated `list`, `show`, and `status` to expose `next_run_at`, `priority`,
  `queue`, `scheduled` count, and per-queue counts.
- Made `work --drain` lane-aware: ignores other lanes and never-run user-delayed
  future rows while preserving retry-backoff draining.

Compatibility: schema version `4`. Existing databases migrate forward.

## v0.3.0 — Agent-Native Ergonomics

Differentiation begins. `CONTRACT_VERSION` remains `1`.

- Added `brief`: compact, token-budgeted usage doc for agent context injection.
- Added `schema`: formal JSON Schema export for the envelope, verb payloads,
  and event stream records.
- Added idempotent submission with `add --key K`: same key while queued/running
  is a no-op returning the existing id.
- Added tags and notes: `add --tag KEY=VALUE`, `add --note STRING`, filterable
  in `list --tag`.
- Added `events`: read the append-only event log as a cursored one-shot,
  long-poll with `--wait`, or tail with `--follow --json` raw NDJSON.

Compatibility: schema version `3`. Existing databases migrate forward.

## v0.2.0 — Operational Completeness

Table-stakes completion of the CLI surface.

- Added `list` and `show <id>` with full attempt history from the event log.
- Added `cancel <id>` to dequeue a queued job; `--running` kills the live
  process group.
- Added `wait <id...>` to block until jobs finish, with exit code reflecting
  outcome. Enables submit-many-then-wait as a parallelism primitive.
- Added `work --drain` to run until queue empty, then exit (cron/CI-friendly).
- Added `prune` to delete old done jobs, events, and output files with
  age/state filters.
- Added `--json` on every read command and stable documented exit codes.

## v0.1.0 — The Reliability Core

Initial release.

- `add`, `work`, `status`, `retry`, `output`.
- Atomic claiming via `BEGIN IMMEDIATE`, confirmed-dead reaping with heartbeat
  and pid-reuse guard, exponential backoff, dead-letter state, per-job timeout
  with process-group kill, per-attempt captured output.
- Full concurrency and SIGKILL test suite: no double execution, SIGKILL
  recovery, no false-positive reap of a live worker, timeout kill including
  grandchildren.
- Ships as a single file and as `pip install spoolctl`. Python 3.10+,
  stdlib only, macOS and Linux.
