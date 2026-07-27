# Project Overview

spoolctl is a local job queue with retries, exponential backoff, and crash recovery, built for operators (human or automated) who won't be watching when things fail. It is a single-machine, stdlib-only Python tool whose coordinator is a SQLite database file, not a daemon.

The project lives at [github.com/Ozhiaki/spoolctl](https://github.com/Ozhiaki/spoolctl). The [README](https://github.com/Ozhiaki/spoolctl#readme) has the full pitch: problem statement, design philosophy, comparison table, interface preview, and FAQ.
