# Data2Agent

**Take an immutable scientific dataset and expose it through a reproducible,
evidence-preserving MCP interface.**

Data2Agent turns a dataset directory into a deterministic manifest, an evidence
ledger, and a generic MCP server that any coding agent can drive — without an
agent ever having to guess at the dataset's structure, and without being able to
invent a value the dataset does not state.

> Architecture inspired by / derived in part from **Paper2Agent**
> Miao et al., *Nature*, 2026 — see [`NOTICE.md`](NOTICE.md).

---

## Status: v0.1 — the deterministic core

```text
dataset
  ↓
deterministic ingest      no LLM · no dependencies · read-only
  ↓
Data2MCP                  six tools + resources · host-agnostic
  ↓
one agent                 no subagents, on purpose
```

FAIR assessment, curation, semantics and multi-agent orchestration are specified
and scheduled, not shipped. See [`docs/ROADMAP.md`](docs/ROADMAP.md) and
[`docs/BACKLOG.md`](docs/BACKLOG.md).

## Quick start

```bash
pip install -e '.[mcp,dev]'

# 1. Ingest — deterministic, read-only over the dataset
data2agent ingest examples/preclinical-minimal -o ./preclinical-agent

# 2. Connect the server (the exact command for your host is in mcp/USAGE.md)
claude mcp add data2agent -- python -m data2agent.cli serve ./preclinical-agent

# 3. Or check the dataset has not drifted since ingest
data2agent verify ./preclinical-agent
```

Output:

```text
./preclinical-agent/
├── manifest.json     what the dataset IS   (deterministic; no timestamps)
├── provenance.json   what this run was     (time, host, tool version)
├── evidence.json     claim → evidence → file → checksum
├── mcp/              server definition + USAGE.md for any MCP host
└── report/           evidence-backed Markdown; every fact carries a claim id
```

## What makes it different

### Ingestion carries no language model

`src/data2agent/ingest/` is deterministic, stdlib-only and read-only. Row
counts, column headers, missingness, checksums and format signatures are
computable exactly — and an exact answer beats a confident-sounding one. An LLM
that infers a dataset's structure is an LLM that can invent one.

Repeated ingests of the same bytes produce **byte-identical** manifests. Two
agent configurations are only comparable at equal `dataset_id`.

### Unknown stays unknown

> Never infer a value the dataset does not state.

Not animal sex, not strain, not acquisition device, not experimental condition.
Concretely:

- An empty cell is **missing** and is counted.
- A literal `NA` is **a token**, counted separately — nothing states what it
  means here, and folding the two together would be inference dressed as
  arithmetic.
- `relationships: []` means *not determined*; the API says so explicitly.
- A detected DOI is a pattern match, never a claim that it resolves.
- A `distinct` count above the enumeration cap is labelled `distinct_exact: false`.

`tests/evidence/test_no_invented_metadata.py` asserts this negatively, by
demanding plausible-but-unstated terms are absent from the output.

### Every fact carries its evidence

```text
claim → evidence → source file → immutable checksum
```

```json
{
  "claim_id": "clm_9f2c4b1a77e0d35c",
  "claim": "'sex' is missing for 12 of 48 row(s) in 'animals.csv'",
  "evidence": [{
    "source": "animals.csv",
    "source_sha256": "3b1f…",
    "check": "table.missing-value-count",
    "result": 12,
    "field": "sex"
  }]
}
```

A claim with no evidence is rejected at write time, and so is a check id that is
not in the published registry. That is what makes *unsupported-claim rate* an
automatically computable metric rather than a matter of reviewer opinion — see
[`docs/evidence-contract.md`](docs/evidence-contract.md).

### One generic server, not a generated one

Datasets share a small set of useful operations, so v0.1 ships a single fixed
server whose behaviour is testable and comparable across datasets — rather than
generating bespoke tools per dataset and inheriting the generator's variance.

## The MCP surface

| Tool | Returns |
| --- | --- |
| `dataset_inventory()` | identity, file count, formats, warnings |
| `list_files(pattern, file_format)` | inventoried files, filtered |
| `inspect_file(path)` | size, checksum, format, bounded preview |
| `inspect_table(path)` | rows, columns, observed shapes, missingness |
| `get_metadata(path)` | recognised metadata files, served verbatim |
| `get_evidence(...)` | what supports a claim |
| `resolve_identifier(value)` | where an identifier occurs (no network call) |

Resources: `dataset://manifest`, `dataset://metadata`, `dataset://provenance`,
`dataset://evidence`, `dataset://files/{path}`.

Every tool re-checksums a file before returning its content, and withholds it on
a mismatch. An answer drawn from drifted bytes is worse than no answer, because
it is indistinguishable from a good one.

## Benchmark modes

The experiment is **harness × orchestrator × model × constraint**. The
constraint axis is built in from v0.1:

```bash
data2agent modes
```

| Mode | Agent is given | Status |
| --- | --- | --- |
| `raw` | files only — the control condition | v0.1 |
| `structured` | the full deterministic surface | v0.1 |
| `fair-skill` | `structured` + FAIR prose | planned |
| `fair-rules` | `structured` + machine-readable rules | planned |
| `fair-deterministic` | `structured` + deterministic FAIR tools | planned |
| `fair-semantic` | + vocabularies and SHACL | planned |

Requesting an unimplemented mode **fails** rather than falling back — a silent
downgrade would produce a run labelled `fair-rules` whose agent never saw a
rule. See [`docs/benchmark-contract.md`](docs/benchmark-contract.md).

The MCP layer knows nothing about Claude Code, Codex, Goose, Pi or OpenCode.
That is a correctness requirement: if host-specific behaviour accumulates below
the boundary, the benchmark stops measuring hosts and starts measuring our
accommodations of them.

## Using it from a coding agent

Install the portable skill into any skill-capable host:

```bash
# Claude Code
mkdir -p "$HOME/.claude/skills/data2agent"
cp -R skills/data2agent/. "$HOME/.claude/skills/data2agent/"

# Codex
mkdir -p "$HOME/.agents/skills/data2agent"
cp -R skills/data2agent/. "$HOME/.agents/skills/data2agent/"
```

Then:

```text
Use the data2agent skill to ingest this dataset and report what it contains.
Report only what the evidence ledger supports.

Dataset: <DATASET_DIR>
Output directory: <OUTPUT_DIR>
```

## Documentation

| Document | Covers |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | the layers, and why each boundary exists |
| [`docs/data-contract.md`](docs/data-contract.md) | input/output contract; what every field is allowed to mean |
| [`docs/evidence-contract.md`](docs/evidence-contract.md) | the claim → evidence invariant and its enforcement |
| [`docs/benchmark-contract.md`](docs/benchmark-contract.md) | modes, comparability rules, metrics |
| [`docs/fair-profile-contract.md`](docs/fair-profile-contract.md) | the canonical FAIR rule format (v0.2) |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | v0.1 → v0.6 |
| [`docs/BACKLOG.md`](docs/BACKLOG.md) | numbered work items with acceptance criteria |

## Development

```bash
pip install -e '.[mcp,dev]'
pytest          # 75 tests
ruff check src tests && ruff format --check src tests
```

Tests are grouped by layer — `tests/ingestion/`, `tests/evidence/`, `tests/mcp/`
— and the acceptance criteria for v0.1 map onto them one-to-one in
[`docs/ROADMAP.md`](docs/ROADMAP.md).

## Related repositories

```text
Data2Agent            this repository: the tool and the instrument
Data2AgentBench       datasets, tasks, harness adapters, scoring
FAIR-VCG-Dataspace    the data space itself
```

Kept separate from the start, following the upstream project's own separation of
Paper2Agent from Paper2AgentBench.

## Licence and attribution

MIT — see [`LICENSE`](LICENSE) and [`NOTICE.md`](NOTICE.md). The upstream
Paper2Agent skill is retained unmodified under `skills/paper2agent/` as the
reference for the v0.5 multi-agent port.

```bibtex
@article{miao2026paper2agent,
  title={Reimagining research papers as interactive and reliable {AI} agents},
  author={Miao, Jiacheng and Davis, Joe R. and Zhang, Yaohui and Pritchard, Jonathan K. and Zou, James},
  journal={Nature},
  year={2026},
  doi={10.1038/s41586-026-11044-y},
  url={https://www.nature.com/articles/s41586-026-11044-y}
}
```
