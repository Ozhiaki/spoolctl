# Landscape

Where spoolctl fits in the spectrum of tools that run commands.

| Layer | Job it leads with | Tools | Coordinator |
|---|---|---|---|
| Terminal multiplexer / shell job control | Keep a command alive across a session | tmux, screen, `nohup`, `&` | none (the TTY) |
| Interactive queue | Serialize commands for a watching human | pueue, task-spooler, nq | daemon |
| Unattended local queue | Survive the operator's death | spoolctl | the SQLite file |
| Application job queue | Queue functions in an app runtime | Celery, RQ, huey, pg-boss | broker or DB + app |
| Workflow engine / scheduler | Orchestrate DAGs, schedules, fleets | Airflow, Temporal, systemd timers, cron | server/cluster |

The differentiating question is: **what happens when nobody is at the keyboard?**

- A terminal multiplexer keeps the process alive, but no one monitors it, retries it, or notices when it fails.
- An interactive queue runs the next command, but its daemon is a single point of failure and it has no retry semantics.
- spoolctl runs the next command, retries failures with backoff, recovers from crashes, and records everything in a database that the next operator (human or agent) can inspect.
- An application queue does all of that and more, but requires an application runtime, a broker, and a deployment.
- A workflow engine orchestrates across machines and time, but requires a server.

spoolctl occupies the gap between "I started a tmux session and hope for the best" and "I deployed Celery with Redis and a worker fleet." It is the simplest tool that provides real reliability guarantees for shell commands on a single machine.
