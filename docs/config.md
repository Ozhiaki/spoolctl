# Config

## Database path resolution

spoolctl resolves the queue database path using this precedence chain:

| Priority | Source | Example |
|---|---|---|
| 1 (highest) | `--db` flag | `--db ./my-queue.db` |
| 2 | `SPOOLCTL_DB` environment variable | `export SPOOLCTL_DB=./my-queue.db` |
| 3 | `.spoolctl/config.json` `db_path` key | `{"db_path": "data/queue.db"}` |
| 4 (lowest) | Default | `.spoolctl/queue.db` |

The default path is relative to the discovery base (typically the current directory).

`config-show` reports which source won and the effective path, so you can always tell how a path was resolved.

## Project config file

`.spoolctl/config.json` is an optional project-level config file. Format:

```json
{
  "version": 1,
  "db_path": "data/queue.db"
}
```

Supported keys:

| Key | Required | Description |
|---|---|---|
| `version` | No | Schema version of the config file. Must be `1` if present. |
| `db_path` | No | Path to the queue database, relative to the config file's directory. |

Unsupported keys are ignored and reported by `config-show`.

A missing config file is a valid state. spoolctl does not require project config to function; the default resolution chain always produces a usable database path.

## `config-show`

`config-show` is a read-only diagnostic that reports the effective configuration without opening or creating the database.

```bash
python3 -m spoolctl config-show --json
```

Fields in the JSON output:

| Field | Description |
|---|---|
| `config_path` | Path to the config file (may not exist). |
| `config_exists` | Whether the config file exists. |
| `config_valid` | Whether the config file is valid JSON with a supported structure. |
| `db_path` | The effective database path after resolution. |
| `db_source` | Which source won: `flag`, `environment`, `project_config`, or `default`. |
| `precedence` | The full precedence chain. |
| `values` | The resolved values. |
| `ignored_keys` | Any unsupported keys found in the config file. |

## `config-validate`

`config-validate` validates the project config file without opening or creating the database.

```bash
python3 -m spoolctl config-validate --json
python3 -m spoolctl config-validate path/to/config.json --json
```

Checks:

- File exists and is valid JSON.
- Top-level value is an object.
- `version` field, if present, equals `1`.
- No unsupported keys (reported as `INVALID_INPUT`).

A missing config file is valid (exit 0). A malformed file or unsupported keys produce `INVALID_INPUT` (exit 1).

Neither `config-show` nor `config-validate` opens or creates the queue database. They are safe to run before any jobs exist.
