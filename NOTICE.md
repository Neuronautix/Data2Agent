# Notice and attribution

Data2Agent is derived in part from **Paper2Agent**.

> Architecture inspired by / derived in part from Paper2Agent
> Miao, J., Davis, J. R., Zhang, Y., Pritchard, J. K. & Zou, J.
> *Reimagining research papers as interactive and reliable AI agents.*
> Nature (2026). doi:10.1038/s41586-026-11044-y

Upstream repository: <https://github.com/jmiao24/Paper2Agent>
Upstream licence: MIT, Copyright (c) 2025 Jiacheng Miao — reproduced in `LICENSE`.

## What is reused

| From Paper2Agent | Used in Data2Agent | Status |
| --- | --- | --- |
| The portable-skill packaging model (a `skills/<name>/` folder installable into any skill-capable host) | `skills/data2agent/` | Adopted |
| "The deliverable is a tested MCP server, not a chat transcript" | `src/data2agent/mcp/`, generated `mcp/USAGE.md` | Adopted |
| Host-agnosticism: the same skill runs under Claude Code, Codex and others | `docs/benchmark-contract.md` | Adopted and tightened into an explicit MCP boundary |
| Coordinator + specialist subagents + independent verifier | Planned for v0.5 | Deferred on purpose — see `docs/BACKLOG.md` |
| `skills/paper2agent/` (the upstream skill tree, unmodified) | Retained in this repository | Reference for the v0.5 port; not part of the Data2Agent MVP |

## What is deliberately different

Paper2Agent's hard problem is discovery: arbitrary research software exposes
arbitrary functionality, so agents must read tutorials and execute code to find
out what a repository can do. Data2Agent's hard problem is the opposite —
datasets are *inert*, and almost everything worth knowing about their structure
can be computed exactly. So:

1. **Ingestion carries no language model.** `src/data2agent/ingest/` is
   deterministic and dependency-free. An LLM that infers a dataset's structure
   is an LLM that can invent one.
2. **One generic MCP server, not a generated per-dataset one.** Paper2Agent
   generates bespoke tools because each repository differs. Datasets share a
   small set of operations, so v0.1 ships a single server whose behaviour is
   fixed and testable.
3. **Evidence is a first-class output.** Every claim resolves to a named check
   over checksummed bytes (`docs/evidence-contract.md`).
4. **Multi-agent orchestration comes last, not first.** See `docs/BACKLOG.md`.

Data2AgentBench and FAIR-VCG-Dataspace are kept in separate repositories, following
the upstream project's own separation of Paper2Agent from Paper2AgentBench.
