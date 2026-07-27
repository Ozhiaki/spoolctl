# spoolctl Documentation

## Start Here

| I want to... | Page |
|---|---|
| Install and run spoolctl | [Install](install.md) |
| Run my first job and see crash recovery | [Quickstart](quickstart.md) |
| Understand jobs, attempts, and states | [Concepts](concepts.md) |
| Know what spoolctl guarantees (and what it doesn't) | [Guarantees](guarantees.md) |
| See how the pieces fit together | [Architecture](architecture.md) |
| Schedule jobs with delays, priorities, or named queues | [Scheduling](scheduling.md) |
| Understand how commands are executed | [Execution](execution.md) |
| Configure the database path and project settings | [Config](config.md) |
| Check if spoolctl is ready to run | [Doctor](doctor.md) |
| Stream or poll the event ledger | [Events](events.md) |
| Build an agent or script on top of spoolctl | [Agent Guide](agent-guide.md) |
| Understand the JSON envelope contract | [JSON Contract](json-contract.md) |
| Look up a verb's flags, exit codes, and output | [Verb Reference](verbs.md) |
| Look up an error code or failure reason | [Error Reference](errors.md) |
| Look up limits, env vars, or global flags | [Limits Reference](reference/limits.md) |
| Look up job states, event types, or scheduling/execution details | [States Reference](reference/states.md) |
| Compare spoolctl to other tools | [Comparison](comparison.md) |
| See where spoolctl fits in the tooling landscape | [Landscape](landscape.md) |
| Read the FAQ | [FAQ](faq.md) |
| Understand the security model | [Security](security.md) |
| Learn about the Ozhiaki family | [Lineage](lineage.md) |

## Pages by Topic

### Getting Started

- [Install](install.md) -- checkout, single-file build, Python version
- [Quickstart](quickstart.md) -- five minutes from zero to crash recovery

### Concepts and Guarantees

- [Concepts](concepts.md) -- jobs, attempts, states, lanes, priorities
- [Guarantees](guarantees.md) -- atomic claiming, crash recovery, at-least-once
- [Architecture](architecture.md) -- SQLite as the daemon, WAL mode, schema history

### Using It

- [Scheduling](scheduling.md) -- delays, priorities, named queues, drain
- [Execution](execution.md) -- argv vs shell, env vars, timeouts, process groups
- [Config](config.md) -- database path resolution, project config
- [Doctor](doctor.md) -- readiness checks and the exit-3 contract
- [Events](events.md) -- the append-only ledger, cursors, follow mode

### Machine Interface

- [Agent Guide](agent-guide.md) -- building agents and scripts on spoolctl
- [JSON Contract](json-contract.md) -- envelope, output modes, totality
- [Verb Reference](verbs.md) -- every verb, flag, and exit code
- [Error Reference](errors.md) -- error codes, exit codes, failure reasons
- [Limits Reference](reference/limits.md) -- numeric limits, env vars, global flags
- [States Reference](reference/states.md) -- job states, attempt states, events

### Positioning

- [Comparison](comparison.md) -- spoolctl vs pueue, task-spooler, nq, and others
- [Landscape](landscape.md) -- where spoolctl fits in the tooling layers
- [FAQ](faq.md) -- common questions and honest answers

### Project

- [Project Overview](repo.md) -- what spoolctl is, where the code lives
- [Security](security.md) -- threat model for a tool that runs commands
- [Lineage](lineage.md) -- the Ozhiaki family and make-cli
