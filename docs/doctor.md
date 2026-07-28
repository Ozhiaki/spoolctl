# Doctor

`doctor` is a bounded readiness diagnostic for agents and automated launchers. It checks whether spoolctl can operate against the configured database without actually opening or mutating it in production mode.

## The seven checks

| Check | What it verifies |
|---|---|
| `config_valid` | The project config file (if present) is valid JSON with supported structure. |
| `db_path_resolved` | The database path resolves successfully through the precedence chain. |
| `spool_directory_writable` | The directory that would contain the database is writable. |
| `database_exists` | The database file exists on disk. |
| `sqlite_open_readwrite` | The database can be opened for read/write via SQLite (without mutating it). |
| `schema_version` | The on-disk schema version matches the running binary's expected version. |
| `contract_metadata` | The capabilities and schema generators produce valid JSON. |

If `config_valid` fails, checks 2-6 are skipped with `blocked_by: "config_valid"`.

## Exit contract

This is the part that trips up every consumer:

| Condition | Exit code | `ok` | `errors` | `data.ready` |
|---|---|---|---|---|
| All checks pass | 0 | `true` | `[]` | `true` |
| Any check fails | 3 | `true` | `[]` | `false` |

Exit 3 with `ok:true` and empty `errors[]` is deliberate. Doctor readiness failures are domain outcomes, not tool errors. The tool call succeeded; the readiness check found a problem.

**JSON consumers** should key off `data.ready` and `data.checks`, not `errors`.

**Shell consumers** should key off exit code 3.

Do not check `ok` or `errors` for doctor results. A readiness failure is not a tool error.

## What doctor does not do

Doctor is read-only and bounded:

- It does not initialize a database.
- It does not run migrations.
- It does not repair a corrupted database.
- It does not mutate any state.

If doctor reports `schema_version` as failed (e.g., the database was created by a newer version of spoolctl), the fix is to update spoolctl, not to run doctor again.

## JSON output

```bash
python3 -m spoolctl doctor --db ./queue.db --json
```

The `data` object contains:

| Field | Description |
|---|---|
| `ready` | `true` if all checks passed, `false` otherwise. |
| `summary` | Counts of `passed`, `failed`, `skipped` checks. |
| `config` | Effective config details (path, existence, validity, db source). |
| `checks` | Array of check results, each with `name`, `status`, and optional `detail`/`blocked_by`. |
| `versions` | `tool_version`, `contract_version`, `schema_version`. |

## Usage in automated launchers

Doctor is the recommended pre-flight check for any automated consumer. The pattern:

1. Run `spoolctl doctor --db <path> --json`.
2. Parse the envelope. Check `data.ready`.
3. If `false`, inspect `data.checks` for the failing check and its `detail`.
4. If `true`, proceed with queue operations.

Doctor is cheap (no database write lock, no table scans) and idempotent. It can be called on every startup without performance concern.
