# Security

spoolctl's entire function is executing arbitrary shell commands. This page documents the security model and its boundaries.

## Execution model

spoolctl executes whatever it is given, as the invoking user, with no sandbox. There is no permission model, no access control, and no execution policy beyond what the operating system provides.

- `add -- <command>` stores an argv vector and executes it verbatim.
- `add -c '<string>'` is shell-string execution: the string is passed to `sh -c`, and its risks are the shell's (injection, expansion, globbing).
- Commands run in the worker's process context with the worker's UID/GID.
- `--cwd` sets the working directory. `--env K=V` sets environment variables.

Anyone who can submit a job can execute arbitrary commands as the user running the worker.

## Environment variable storage

`--env K=V` values are stored in the database in plaintext. They are visible to anyone who can read the database file or run `show`:

- `add` and `list` expose env key *names* only.
- `show` exposes full key-value pairs.

Secrets belong in the process environment or a secret store, not in `--env`. If a job needs a secret, pass it through the environment at `work` time (the worker's `os.environ` is inherited by child processes) or reference a secret store from within the command.

## Database file permissions

The queue database (`queue.db`) carries the privileges of its filesystem permissions. Anyone who can write the database file can:

- Schedule commands that the next worker will execute.
- Modify existing job state (cancel, retry, prune).
- Read captured stdout/stderr of completed jobs.

Protect the database file with appropriate filesystem permissions. On a multi-user system, ensure the database and its spool directory are readable and writable only by the intended user.

## Output persistence

Captured stdout and stderr persist in the spool directory until explicitly removed by `prune`. Output files are not encrypted and are readable by anyone with filesystem access to the spool directory.

## Network surface

spoolctl has no network surface. It does not listen on any port, does not make outbound connections, and does not expose any API. All communication is through the local SQLite file.

## Vulnerability reporting

Report security issues via GitHub issues at <https://github.com/Ozhiaki/spoolctl/issues>. spoolctl is a pre-release tool; there is no formal vulnerability disclosure policy at this time.
