---
title: Lineage
description: Learn how spoolctl relates to the Ozhiaki tool family.
bucket: project
order: 50
---

# Lineage

spoolctl is part of the Ozhiaki family of CLI tools:

| Tool | Purpose |
|---|---|
| [spoolctl](https://github.com/Ozhiaki/spoolctl) | Local job queue with retries, backoff, and crash recovery. |
| [inferctl](https://github.com/Ozhiaki/inferctl) | Batch inference orchestration for LLM pipelines. |
| [evalctl](https://github.com/Ozhiaki/evalctl) | Evaluation harness for LLM output quality. |

All three conform to the [make-cli](https://github.com/Ozhiaki/make-cli) contract discipline: structured JSON envelopes, totality guarantees (no traceback, no hang), closed error-code registries, and machine-discoverable capabilities. Conformance means an agent that has learned to consume one tool's `--json` output can apply the same patterns to the others.

The tools compose naturally: spoolctl manages execution, inferctl manages inference requests, and evalctl evaluates results. A pipeline can use spoolctl to queue inference jobs, inferctl to run them, and evalctl to score the output.
