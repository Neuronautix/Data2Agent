---
name: data2agent
description: Turn an immutable scientific dataset into a reproducible, evidence-preserving MCP interface, then answer questions about it without inventing anything. Use for dataset ingestion, dataset inspection, FAIR-oriented curation groundwork, and any task where a claim about data must be traceable to bytes.
---

# Data2Agent

Take an immutable scientific dataset and expose it through a reproducible,
evidence-preserving MCP interface.

This skill is **not** a paper-to-agent converter. A dataset's structure is a
property of its bytes, so it is computed exactly rather than discovered by an
agent. Your job starts after that computation, and is bounded by it.

## The rule that overrides everything else

> **Never state something the dataset does not state.**

Not animal sex, not strain, not acquisition device, not experimental condition,
not units — however plausible, however much the surrounding context suggests it.
"Unknown" is a complete and correct answer. Filling a gap with a likely value is
the single failure mode this skill exists to prevent.

Read [references/evidence-discipline.md](references/evidence-discipline.md)
before reporting anything.

## Workflow

### 1. Ingest — deterministic, no model involved

```bash
data2agent ingest <DATASET_DIR> -o <OUTPUT_DIR>
```

This is read-only over the dataset and writes:

```text
<OUTPUT_DIR>/
├── manifest.json     what the dataset IS (deterministic; no timestamps)
├── provenance.json   what this run was
├── evidence.json     claim → evidence → file → checksum
├── mcp/              server definition + USAGE.md
└── report/           evidence-backed Markdown
```

Do not hand-write any of these, do not edit them afterwards, and do not write
anything into the dataset directory. If the output directory would sit inside
the dataset, the command refuses — pick another location rather than working
around it.

### 2. Connect the MCP server

Follow `<OUTPUT_DIR>/mcp/USAGE.md`. It contains the exact command for the host
you are running in. The server speaks MCP over stdio and behaves identically on
every host.

### 3. Answer through the tools

| Tool | Use for |
| --- | --- |
| `dataset_inventory` | orient: identity, file count, formats, warnings |
| `list_files` | find files by glob or format |
| `inspect_file` | size, checksum, detected format, bounded preview |
| `inspect_table` | rows, columns, observed token shapes, missingness |
| `get_metadata` | recognised metadata files, served verbatim |
| `get_evidence` | **what actually supports a statement** |
| `resolve_identifier` | where an identifier occurs (no network resolution) |

Details and traps: [references/mcp-tools.md](references/mcp-tools.md).

### 4. Report

Every statement cites a claim id from `get_evidence`. Anything you could not
establish goes under an explicit "not determined" heading. See
[references/reporting.md](references/reporting.md).

## Reading the output correctly

An empty value means *not determined*, never *none*:

| You see | It means | It does **not** mean |
| --- | --- | --- |
| `relationships: []` | this version does not compute relationships | the dataset has none |
| `metadata_files: []` | no filename matched a known convention | the dataset has no metadata |
| `column.missing: 12` | 12 **empty** cells | 12 unknown values |
| `null_like_tokens: 3` | 3 cells hold a token like `NA` | 3 missing values |
| `distinct_exact: false` | `distinct` is an upper bound | the exact cardinality |
| `detected_by: "extension"` | the filename said so; the bytes did not | a verified format |
| `format: "unknown"` | nothing matched | the file is corrupt |

## Scope

In scope today: ingestion, inspection, evidence-bound reporting over local
dataset directories.

**Out of scope — say so rather than improvising:**

- FAIR assessment and scoring (a separate profile, planned).
- Resolving or validating identifiers over the network.
- Controlled vocabularies, ontology mappings, SHACL.
- Modifying or curating the dataset.
- Inferring cross-file relationships.

If asked for any of these, state that it is not implemented and offer what the
deterministic layer *can* establish. Do not approximate an unimplemented check
with your own judgement — an approximation is indistinguishable from the real
thing in the output, which is exactly the problem.
