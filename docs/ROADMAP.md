# Roadmap

One rule governs the ordering: **do not debug ingestion, MCP, FAIR logic and
multi-agent orchestration at the same time.** Each version stabilises one layer
before the next is allowed to depend on it.

```text
v0.1  deterministic core ····································· shipped
      dataset → deterministic ingest → Data2MCP → one agent

v0.2  + FAIR profile, rule registry, deterministic FAIR checks ··· shipped
v0.3  + prose projection (fair-skill), curated export, remote sources
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

Acceptance tests, all green (`tests/`, 139 tests across v0.1 and v0.2):

| ✓ | Criterion | Covered by |
| --- | --- | --- |
| ✓ | source bytes unchanged | `ingestion/test_immutability.py` |
| ✓ | every source file checksummed | `ingestion/test_immutability.py` |
| ✓ | deterministic repeated ingest gives the same manifest | `ingestion/test_determinism.py` |
| ✓ | table schema correctly extracted | `ingestion/test_tabular.py` |
| ✓ | missingness correctly calculated | `ingestion/test_tabular.py`, `ingestion/test_conventions.py` |
| ✓ | no invented metadata | `evidence/test_no_invented_metadata.py` |
| ✓ | every reported fact references evidence | `evidence/test_ledger.py` |
| ~ | MCP works from both Codex and Claude Code | `mcp/test_server_binding.py` covers the protocol surface in-process; the two-host manual check is **D2A-15**, still open |

## v0.2 — FAIR as a separable profile · shipped

12 canonical rules in `src/data2agent/profiles/fair/rules/`, 10 deterministic
implementations, the `fair-rules` and `fair-deterministic` modes, and
`data2agent assess`. `Data2MCP ≠ FAIR checker` still holds: `--mode structured`
exposes no FAIR concept, asserted by a test and by an import-layering check.

The two rules that need the network or the dataset's published location stay in
the registry and return `unknown`. A dropped rule leaves the denominator; an
unknown one stays countable.

Also in v0.2, from the same release:

- Missing-value conventions. `NA` now reads as missing, under a **named**
  convention that is stored in the manifest and cited by every missingness
  claim — a declared rule rather than a judgement call.
- Ingest timestamps surfaced through `dataset_inventory()`, `get_provenance()`,
  `dataset://provenance` and the report, while `manifest.json` stays
  timestamp-free so repeat ingests still compare byte-for-byte.

## v0.3 — prose projection, curation, remote sources

Generate the `fair-skill` Markdown from the same canonical rules, so three of the
four FAIR modes go live and prose-vs-rules-vs-deterministic becomes runnable.

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
