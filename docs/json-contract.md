# JSON Contract

## Envelope structure

Every `--json` response is a single JSON object with these top-level fields:

| Field | Type | Description |
|---|---|---|
| `ok` | boolean | `true` if `errors` is empty, `false` otherwise. |
| `tool_version` | string | The spoolctl version that produced this envelope. |
| `data` | object or null | The verb's result payload. `null` on failure. |
| `meta` | object | Request metadata: `request_id`, `ts_iso`, `elapsed_ms`, `contract_version`, `data_hash`. May include `pagination` or `wait` sub-objects. |
| `warnings` | array | Warning objects. May be non-empty even when `ok` is `true`. |
| `commands` | array | Reserved for future use. Always `[]` in the current contract. |
| `errors` | array | Error objects. Non-empty implies `ok: false`. |

On failure, the same envelope structure is used with `data: null` and populated `errors`. No verb ever produces a raw error string outside the envelope.

## Output modes

spoolctl has four output modes. The mode is determined by flags and context:

| Mode | Trigger | Output |
|---|---|---|
| `envelope` | `--json` | A single JSON envelope on stdout. |
| `frames` | `events --follow --json` | NDJSON: one JSON object per line. Data frames interleaved with control frames. |
| `raw` | `output --raw` | Raw bytes of captured stdout/stderr, written directly to stdout. |
| `text` | No `--json` flag (default) | Human-readable text. Format may change between versions. |

`envelope` is the stable machine interface. `text` is for humans and is not part of the contract.

`frames` mode skips envelope emission entirely. The stream consists of data frames (event objects) and control frames (`{"control": {"type": ..., "reason": ...}}`). See [Events](events.md).

`raw` mode writes captured output bytes directly to stdout with no framing. It is mutually exclusive with `--json`.

## Totality guarantees

spoolctl's contract requires total, structured responses for all inputs:

- **No traceback.** Every unexpected exception is caught and converted to a structured `INTERNAL` error with exit code 3. Python tracebacks never reach stdout or stderr.
- **No hang.** Every code path that blocks has a timeout.
- **Structured errors for malformed input.** Invalid types, out-of-range values, bad durations, bad timestamps, bad env var syntax, and bad enum values all produce `INVALID_INPUT` errors with exit code 1.
- **Flag abbreviation disabled.** All parsers are constructed with `allow_abbrev=False`. `--js` does not expand to `--json`.
- **Inert flags rejected.** Unrecognized flags produce `UNKNOWN_FLAG` (exit 1) with a `did_you_mean` suggestion, not a silent pass-through.

These guarantees mean a consumer can always `json.loads()` the stdout of a `--json` command and get a well-formed envelope, even for inputs the consumer did not anticipate.

## Error and warning objects

Error objects in `errors[]`:

```json
{
  "code": "INVALID_INPUT",
  "message": "...",
  "exit_code": 1,
  "detail": {}
}
```

Warning objects in `warnings[]`:

```json
{
  "code": "IDEMPOTENCY_METADATA_DIFFERS",
  "message": "..."
}
```

See [Error Reference](errors.md) for the full code registry.

## `CONTRACT_VERSION` policy

`CONTRACT_VERSION` (currently `"2"`) is a semantic marker for the contract surface. It changes when the contract makes a breaking change. The tool version changes on every release; the contract version changes rarely.

The current policy: contract version 2 is the pre-release hardening contract. It intentionally breaks v1 quirks (refusing unsafe operations, rejecting inert flags, enforcing structured malformed-input errors, declaring all four output modes, adding config and doctor). No v1 compatibility shim is provided before public release.

Consumers that depend on specific contract behavior should check `meta.contract_version` and fail explicitly if it changes.

## Consuming `schema --json`

`schema --json` provides the structural schema for envelopes and per-verb data payloads. Use it to validate responses programmatically or to generate client bindings. See [Agent Guide](agent-guide.md).
