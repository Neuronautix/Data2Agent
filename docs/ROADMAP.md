# Roadmap

One rule governs the ordering: **do not debug ingestion, MCP, FAIR logic and
multi-agent orchestration at the same time.** Each version stabilises one layer
before the next is allowed to depend on it.

```text
v0.1  deterministic core ····································· shipped
      dataset → deterministic ingest → Data2MCP → one agent

v0.2  + FAIR profile, rule registry, deterministic FAIR checks
v0.3  + curated export, provenance of every modification, remote sources
v0.4  + controlled vocabularies, ontology mappings, SHACL
v0.5  + multi-agent workflow (the Paper2Agent pattern, ported)
v0.6  + benchmark harness adapters (in Data2AgentBench)
```

## v0.1 — deterministic core · shipped

```text
dataset
  ↓
deterministic ingest      no LLM, no dependencies, read-only
  ↓
Data2MCP                  six tools + resources, host-agnostic
  ↓
one agent                 no subagents, no orchestration
```

Six tools: `dataset_inventory`, `list_files`, `inspect_file`, `inspect_table`,
`get_metadata`, `get_evidence` (plus `resolve_identifier`).

Acceptance tests, all green (`tests/`, 75 tests):

| ✓ | Criterion | Covered by |
| --- | --- | --- |
| ✓ | source bytes unchanged | `ingestion/test_immutability.py` |
| ✓ | every source file checksummed | `ingestion/test_immutability.py` |
| ✓ | deterministic repeated ingest gives the same manifest | `ingestion/test_determinism.py` |
| ✓ | table schema correctly extracted | `ingestion/test_tabular.py` |
| ✓ | missingness correctly calculated | `ingestion/test_tabular.py` |
| ✓ | no invented metadata | `evidence/test_no_invented_metadata.py` |
| ✓ | every reported fact references evidence | `evidence/test_ledger.py` |
| ~ | MCP works from both Codex and Claude Code | `mcp/test_server_binding.py` covers the protocol surface in-process; the two-host manual check is **D2A-15**, still open |

## v0.2 — FAIR as a separable profile

The rule registry is the hard part and comes first; the ontology work does not
start here. `Data2MCP ≠ FAIR checker` must still hold when this ships: adding
`--mode structured` must expose no FAIR concept at all.

## v0.3 — curation and remote sources

Export a curated dataset where **every modification carries provenance**. Accept
a DOI, repository URL or archive as input, snapshotted to an immutable local
copy before anything else happens.

## v0.4 — semantics

Controlled vocabularies and ontology mappings, then SHACL over RDF metadata.
Only now, on top of a stable registry.

## v0.5 — multi-agent

Port Paper2Agent's coordinator/specialist/verifier pattern:

```text
Coordinator
├── Inventory agent
├── FAIR assessor
├── Domain metadata specialist
├── Curation agent
└── Independent verifier
```

Deliberately last. Starting here would mean debugging ingestion, MCP, FAIR logic
and orchestration simultaneously, with no reference implementation to compare
against.

## v0.6 — benchmark adapters

Harness adapters live in **Data2AgentBench**, a separate repository. This one
stays a library plus a server.

## Separate repositories

```text
Data2Agent            this repository: the tool and the instrument
Data2AgentBench       datasets, tasks, harness adapters, scoring
FAIR-VCG-Dataspace    the data space itself
```

Kept apart from the start, following the upstream project's own separation of
Paper2Agent from Paper2AgentBench.
