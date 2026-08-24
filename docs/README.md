# spoolctl Documentation

## Start Here

| I want to... | Page |
|---|---|
| Install and run spoolctl | [Install](/docs/install/) |
| Run my first job and see crash recovery | [Quickstart](/docs/quickstart/) |
| Understand jobs, attempts, and states | [Concepts](/docs/concepts/) |
| Know what spoolctl guarantees (and what it doesn't) | [Guarantees](/docs/guarantees/) |
| See how the pieces fit together | [Architecture](/docs/architecture/) |
| Schedule jobs with delays, priorities, or named queues | [Scheduling](/docs/scheduling/) |
| Understand how commands are executed | [Execution](/docs/execution/) |
| Configure the database path and project settings | [Config](/docs/config/) |
| Check if spoolctl is ready to run | [Doctor](/docs/doctor/) |
| Stream or poll the event ledger | [Events](/docs/events/) |
| Build an agent or script on top of spoolctl | [Agent Guide](/docs/agent-guide/) |
| Get a one-call verdict on a finished job | [Agent Guide: feedback](/docs/agent-guide/#one-call-verdict-feedback) |
| Understand the JSON envelope contract | [JSON Contract](/docs/json-contract/) |
| Look up a verb's flags, exit codes, and output | [Verb Reference](/docs/verbs/) |
| Look up an error code or failure reason | [Error Reference](/docs/errors/) |
| Look up limits, env vars, or global flags | [Limits Reference](/docs/limits/) |
| Look up job states, event types, or scheduling/execution details | [States Reference](/docs/states/) |
| Compare spoolctl to other tools | [Comparison](/docs/comparison/) |
| See where spoolctl fits in the tooling landscape | [Landscape](/docs/landscape/) |
| Read the FAQ | [FAQ](/docs/faq/) |
| Understand the security model | [Security](/docs/security/) |
| Learn about the Ozhiaki family | [Lineage](/docs/lineage/) |

## Pages by Topic

### Getting Started

- [Install](/docs/install/) -- checkout, single-file build, Python version
- [Quickstart](/docs/quickstart/) -- five minutes from zero to crash recovery

### Concepts and Guarantees

- [Concepts](/docs/concepts/) -- jobs, attempts, states, lanes, priorities
- [Guarantees](/docs/guarantees/) -- atomic claiming, crash recovery, at-least-once
- [Architecture](/docs/architecture/) -- SQLite as the daemon, WAL mode, schema history

### Using It

- [Scheduling](/docs/scheduling/) -- delays, priorities, named queues, drain
- [Execution](/docs/execution/) -- argv vs shell, env vars, timeouts, process groups
- [Config](/docs/config/) -- database path resolution, project config
- [Doctor](/docs/doctor/) -- readiness checks and the exit-3 contract
- [Events](/docs/events/) -- the append-only ledger, cursors, follow mode

### Machine Interface

- [Agent Guide](/docs/agent-guide/) -- building agents and scripts on spoolctl
- [JSON Contract](/docs/json-contract/) -- envelope, output modes, totality
- [Verb Reference](/docs/verbs/) -- every verb, flag, and exit code
- [Error Reference](/docs/errors/) -- error codes, exit codes, failure reasons
- [Limits Reference](/docs/limits/) -- numeric limits, env vars, global flags
- [States Reference](/docs/states/) -- job states, attempt states, events

### Positioning

- [Comparison](/docs/comparison/) -- spoolctl vs pueue, task-spooler, nq, and others
- [Landscape](/docs/landscape/) -- where spoolctl fits in the tooling layers
- [FAQ](/docs/faq/) -- common questions and honest answers

### Project

- [Project Overview](/docs/repo/) -- what spoolctl is, where the code lives
- [Security](/docs/security/) -- threat model for a tool that runs commands
- [Lineage](/docs/lineage/) -- the Ozhiaki family and make-cli
