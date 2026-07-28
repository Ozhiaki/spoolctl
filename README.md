# spoolctl

<div align="center">

![Status](https://img.shields.io/badge/status-pre--release-orange)
[![CI](https://github.com/Ozhiaki/spoolctl/actions/workflows/ci.yml/badge.svg)](https://github.com/Ozhiaki/spoolctl/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B%20·%20stdlib%20only-blue)
![Platform](https://img.shields.io/badge/platform-macOS%20·%20Linux-lightgrey)
![License](https://img.shields.io/badge/license-Apache--2.0-green)

**A local job queue built for operators that die.**

Daemonless, crash-safe shell job execution, coordinated entirely through one SQLite file.
No broker. No server. No dependencies.

</div>

> **Status: published, pre-1.0.** spoolctl v0.4.11 is on PyPI with contract
> version `2`, local migrations, schemas, generated conformance probes, readiness
> diagnostics, concurrency tests, and [documentation](docs/README.md). The CLI
> surface can still move between minor versions, but the documented interface and
> guarantees are tested in this repository.

---

## The Problem

Every local job-queue tool assumes a human is watching. pueue, task-spooler, and nq give
you a CLI for queueing shell commands — but if the process running your job is SIGKILLed,
the job is just gone. No retry, no backoff, no dead-letter state. *You* were the reliability
layer: you watched the terminal, you reran the failure.

The tools that do have real reliability semantics — Celery, RQ, huey, and the wave of
SQLite/Postgres-backed job queues — are libraries. Jobs are functions in your codebase,
workers belong to an application runtime, and there's a broker or at least a `pip install`
between you and a queue.

That gap didn't matter when a person was at the keyboard. It matters now, because an
increasing share of local shell work is submitted by processes that *cannot* watch:
coding agents, CI-adjacent automation, unattended pipelines. These operators get killed
mid-job as a matter of routine, run concurrently without coordinating, and need work to
survive them.

## The Solution

spoolctl takes the reliability semantics of a serious job queue and gives them to
arbitrary shell commands, with less infrastructure than either shelf:

- **Jobs are shell commands.** No app, no serialized callables, no language coupling.
- **No daemon, no broker.** Workers are symmetric peer processes; all coordination happens
  through one SQLite file. There is no coordinator whose death breaks the queue.
- **Crash-safe at-least-once execution.** A job whose worker is SIGKILLed mid-run is
  reclaimed and retried by any other worker — and never reclaimed while the owning process
  is still alive. Mutual exclusion is absolute; recovery is eventual.
- **Retries, backoff, dead-letter.** Failed jobs retry automatically with exponential
  backoff, then land in a durable `dead` state you can inspect and requeue.
- **Per-job timeouts** with process-group kill — a hung job's entire process tree dies,
  not just its shell.
- **Scheduling-lite and lanes.** Delay jobs with `--after`/`--at`, prioritize eligible
  work, and isolate scarce resources with named queues plus opt-in per-lane slot ceilings.
- **Captured output, retrievable later** — by a different process than the one that
  submitted the job, including after every retry.
- **Zero install surface.** Python 3 standard library only. It's infrastructure you can
  install by writing one file into a sandbox.

## Interface Preview

*Pre-release: this is the committed CLI surface.*

```console
$ spoolctl add -- python fetch.py --all
Added job 1

$ spoolctl add --after 5m --priority 10 --queue gpu -- python train.py
Added job 2

$ spoolctl add --timeout 600 -- ffmpeg -i in.mp4 out.webm
Added job 3

# Start workers anywhere, any time — as many as you like, no coordination needed
$ spoolctl work &
$ spoolctl work &
$ spoolctl work --queue gpu --slots 1 &

$ spoolctl status
canceled 0  dead 0  done 1  failed 0  queued 1  running 1
scheduled 1

$ spoolctl status --json          # machine-readable, for operators without eyes
{"counts": {"queued": 1, "running": 1, "done": 1, ...}, "scheduled": 1, "queues": {...}}

$ spoolctl output 1               # captured stdout/stderr, any time after the run
fetched 3120 records

$ spoolctl feedback 1 --json      # one-call verdict: terminal, succeeded, why, what next
{"state": "done", "terminal": true, "succeeded": true, "remediation": null, ...}

$ spoolctl retry 7                # requeue a dead job with a fresh retry budget
Job 7 requeued

$ spoolctl prune --older-than 30d --dry-run
Would delete 42 jobs

$ spoolctl brief                  # compact agent-facing usage reference
$ spoolctl capabilities --json    # machine-readable verb/flag/mode contract
$ spoolctl schema --json          # formal envelope, verb, and stream schemas
$ spoolctl config-show --json     # effective read-only project configuration
$ spoolctl config-validate --json # validate .spoolctl/config.json without opening DB
$ spoolctl doctor --json          # bounded readiness check for launchers
$ spoolctl robot-docs guide --json # longer agent workflow guide
$ spoolctl events --follow --json --max-events 1 # NDJSON data/control frames
```

## Documentation

Full documentation is in [`docs/`](docs/README.md): install, quickstart,
concepts, guarantees, architecture, scheduling, execution, config, events,
agent guide, JSON contract, verb/error/state reference, and more.

## Design Philosophy

1. **SQLite is the daemon.** Atomic claims via `BEGIN IMMEDIATE`, WAL for concurrent
   readers and writers, durability from fsync. Everything a coordinator process would do,
   the database file does — and it can't crash, hang, or need a launchd unit.
2. **Workers are peers.** Every worker runs crash recovery inline before claiming work.
   No supervisor, no leader, no special first process. If no worker is running, jobs
   simply wait — which is the correct behavior for a tool with no daemon.
3. **Never twice beats always-now.** Where "never execute a job on two workers at once"
   and "recover orphaned jobs quickly" conflict, spoolctl always chooses safety: a job is
   reclaimed only after positive confirmation that its owning process is gone. Recovery
   may be delayed; double-execution is designed out.
4. **The operator is a process, not a person.** Machine-readable status, durable state
   for every outcome, and output that outlives the session that submitted it. Nothing
   assumes a human is watching a terminal.
5. **Distribution is a feature.** Stdlib-only means no dependency resolution, no
   compiler, no package manager access required. Any environment with Python 3 can run
   the queue — including sandboxes where an agent can't `brew install` anything.

## Comparison

|  | spoolctl | pueue | task-spooler | nq | queue libraries¹ |
|---|---|---|---|---|---|
| Jobs are shell commands | ✓ | ✓ | ✓ | ✓ | ✗ (jobs are code) |
| No daemon required | ✓ | ✗ (`pueued`) | ✗ | ✓ | n/a (embedded) |
| Concurrent independent workers | ✓ | within one daemon | ✗ | ✗ | ✓ |
| Job survives SIGKILL of its runner | ✓ | ✗ | ✗ | ✗ | ✓ |
| Automatic retry + backoff | ✓ | ✗ | ✗ | ✗ | ✓ |
| Dead-letter state | ✓ | ✗ | ✗ | ✗ | varies |
| Per-job timeout, process-group kill | ✓ | ✗ | ✗ | ✗ | varies |
| Delays, priorities, named resource lanes | ✓ | partial | partial | ✗ | varies |
| Runtime dependencies | none | Rust binary + daemon | C binary + daemon | C binary | app + pip/broker |

¹ Celery, RQ, huey, litequeue, plainjob, pg-boss, et al. — excellent semantics, but they
are libraries embedded in an application runtime, not standalone tools.

pueue is a great tool if you are a human supervising long-running commands — it has a
richer interactive surface (pause/resume, dependencies, TUI-grade status) and it should
keep that market. spoolctl exists for the operator who won't be there when the job fails.

## How It Works

```
 spoolctl add ──────────────► ┌───────────────────┐
                              │   queue.db         │
 spoolctl work ─┐  claim/     │   (SQLite, WAL)    │ ◄──── spoolctl status
 spoolctl work ─┼─ heartbeat/ │                    │ ◄──── spoolctl output
 spoolctl work ─┘  record     │  jobs · attempts   │ ◄──── spoolctl retry
      │                       └───────────────────┘
      ▼
  fork/exec job in its own process group
  enforce timeout · capture stdout/stderr
```

- **Claiming** is one `BEGIN IMMEDIATE` transaction: workers serialize on SQLite's write
  lock, so a job can never be claimed twice. No advisory locks, no lockfiles.
- **Crash recovery**: running jobs carry a heartbeat and the owner's PID. A stale job is
  reclaimed only after the reaper confirms the owning process is actually dead (with
  pid-reuse protection) — inconclusive always means "leave it alone."
- **Failure handling**: nonzero exit, timeout, or a reaped crash all feed the same path:
  exponential backoff and requeue until the retry budget is spent, then `dead`.
  Per-attempt failure reasons are machine-classifiable via `show --json`.
- **Output** from every attempt is captured and kept — retries don't clobber the
  evidence of what went wrong before.

See the [documentation](docs/README.md) for contract discovery, configuration,
readiness diagnostics, and the full architecture.

## Installation

```console
$ uv tool install spoolctl        # or: pip install spoolctl
```

Zero runtime dependencies, Python 3.10+. Or run from a checkout with
`python -m spoolctl`. For the single-file build, run
`python3 scripts/build_single_file.py` from a checkout — it writes a standalone
`dist/spoolctl.py` that runs anywhere a Python 3 interpreter exists, with nothing
else to install.

Versions are pre-1.0: the CLI contract is tested and documented, but it can still
move between minor versions. `spoolctl capabilities --json` reports the contract
version a given build implements.

## Limitations

Read these before adopting. They are design decisions, not roadmap gaps:

- **At-least-once means at-least-once.** If a worker dies after your command finished but
  before the result was recorded, the command runs again. Make jobs idempotent, or accept
  occasional re-execution. Exactly-once for arbitrary shell commands is not possible, and
  tools that imply otherwise are lying to you.
- **A hung-but-alive worker is not reaped.** If a worker process deadlocks without dying,
  its job stays `running` until you kill the worker. This is the deliberate cost of never
  double-executing — no mechanism can distinguish "slow" from "hung" without false
  positives in one direction, and spoolctl refuses the dangerous direction.
- **One machine, local filesystem.** Coordination correctness comes from SQLite locking;
  NFS and friends are unsupported. No distributed mode, ever — that's a different tool.
- **Scheduling is deliberately small.** One-shot delays, priorities, and named lanes with
  opt-in slot ceilings. No recurring schedules or job dependencies.
- **POSIX only.** macOS and Linux. Process groups and signal semantics are load-bearing;
  Windows is out of scope.

## FAQ

**Why not just use pueue?**
If you're a human watching your queue, do. pueue's daemon is also its weakness for
unattended use: it's a single coordinator with no automatic retry, no backoff, and no
dead-letter state. spoolctl is for work that has to survive nobody watching.

**Why not Celery / RQ / huey?**
Those queue *functions in your application*. spoolctl queues *commands on your machine*.
If you have an app with a worker fleet and a broker, you don't need spoolctl.

**Why SQLite instead of lockfiles or a spool directory?**
Atomic claim-one-of-N under concurrency is exactly what a transactional database does
and exactly what flock choreography does badly. WAL mode makes readers free, and the
whole queue is one inspectable file.

**Why Python stdlib only?**
Zero-dependency single-file Python is the most installable software artifact that exists:
every macOS and Linux box can run it, and an agent can "install" it by writing a file.

## About Contributions

*About Contributions:* Please don't take this the wrong way, but I do not accept outside contributions for any of my projects. I simply don't have the mental bandwidth to review anything, and it's my name on the thing, so I'm responsible for any problems it causes; thus, the risk-reward is highly asymmetric from my perspective. I'd also have to worry about other "stakeholders," which seems unwise for tools I mostly make for myself for free. Feel free to submit issues, and even PRs if you want to illustrate a proposed fix, but know I won't merge them directly. Instead, I'll have Claude or Codex review submissions via `gh` and independently decide whether and how to address them. Bug reports in particular are welcome. Sorry if this offends, but I want to avoid wasted time and hurt feelings. I understand this isn't in sync with the prevailing open-source ethos that seeks community contributions, but it's the only way I can move at this velocity and keep my sanity.

## License

Apache 2.0
