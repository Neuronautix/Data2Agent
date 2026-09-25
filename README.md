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

## Status: v0.2 — deterministic core + FAIR profile

```text
dataset
  ↓
deterministic ingest      no LLM · no dependencies · read-only
  ↓
Data2MCP                  host-agnostic tools + resources
  ↓
FAIR profile              12 canonical rules · 10 deterministic checks
  ↓
one agent                 no subagents, on purpose
```

Curation, semantics (vocabularies, SHACL) and multi-agent orchestration are
specified and scheduled, not shipped. See [`docs/ROADMAP.md`](docs/ROADMAP.md)
and [`docs/BACKLOG.md`](docs/BACKLOG.md).

## Quick start

```bash
pip install -e '.[mcp,fair,dev]'

# 1. Ingest — deterministic, read-only over the dataset
data2agent ingest examples/preclinical-minimal -o ./preclinical-agent

# 2. Assess it against the FAIR profile — verdicts from code, not from a model
data2agent assess ./preclinical-agent

# 3. Connect the server (the exact command for your host is in mcp/USAGE.md)
claude mcp add data2agent -- python -m data2agent.cli serve ./preclinical-agent

# Check the dataset has not drifted since ingest
data2agent verify ./preclinical-agent
```

Output:

```text
./preclinical-agent/
├── manifest.json     what the dataset IS   (deterministic; no timestamps)
├── provenance.json   what this run was     (time, duration, tool version)
├── evidence.json     claim → evidence → file → checksum
├── assessment.json   FAIR results, each citing the evidence behind it
├── mcp/              server definition + USAGE.md for any MCP host
└── report/           evidence-backed Markdown; every fact carries a claim id
```

The example dataset assesses as 8 pass, 2 fail, 2 unknown. Both failures are
real and deliberate; both unknowns are questions a local snapshot cannot settle.

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

- `relationships: []` means *not determined*; the API says so explicitly.
- A detected DOI is a pattern match, never a claim that it resolves.
- A `distinct` count above the enumeration cap is labelled `distinct_exact: false`.
- Two FAIR rules return `unknown` rather than guessing, and stay in the
  denominator while they do.

`tests/evidence/test_no_invented_metadata.py` asserts this negatively, by
demanding plausible-but-unstated terms are absent from the output.

### `NA` is missing — by a declared rule, never a judgement call

A blank cell records no value. A cell holding `NA` records a *token*, and
whether that token means "missing" belongs to the dataset's conventions, not its
bytes. Data2Agent resolves such tokens through an explicit **named convention**
that is stored in the manifest and cited by every missingness claim:

```json
"strain": {
  "missing": 3,
  "missing_empty": 0,
  "missing_sentinel": 3,
  "sentinel_tokens_seen": { "NA": 3 },
  "dtype": "string"
}
```

```text
missing_value_convention:
  id:     default-sentinels
  source: built-in default (the dataset declares no convention of its own)
```

Change the convention and the numbers change — visibly, with the reason
attached:

```bash
data2agent ingest ./ds -o ./out                          # NA → missing (default)
data2agent ingest ./ds -o ./out --strict-missing         # only empty cells
data2agent ingest ./ds -o ./out --missing-tokens NA,-,?  # your call, recorded
```

If the dataset declares its own convention — a Frictionless `missingValues`, say
— that is used instead, and `source` says so.

Two consequences worth knowing:

- A resolved sentinel contributes no type, so `weight_g` holding `18, NA, 22` is
  an `integer` column with one missing value, not a `string` column.
- `unknown`, `-` and `?` are **not** resolved by any built-in convention. A cell
  reading `unknown` may be a considered statement rather than an absence, so
  they are counted in `ambiguous_tokens_seen` and left for a human to rule on.

An undeclared `NA` is a genuine reusability defect — a reader cannot tell "not
measured" from "measured as zero" from a strain literally named NA. The FAIR
profile reports it as `R1.3-MISSING-VALUES-DECLARED`.

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

### FAIR is a profile, not a feature

```text
Core                          Profiles
├── ingestion                 └── fair/
├── evidence                      ├── profile.yaml
└── MCP                           ├── rules/*.yaml   ← the canonical source
                                  └── checks.py      ← one projection of it
```

`Data2MCP ≠ FAIR checker`. One canonical rule is projected into a structured
listing, an executable check, a JSON Schema and (later) prose and SHACL — so the
benchmark can vary the *form* of a constraint while holding its content fixed.
Six independently written rule sets would confound the two.

The loader is strict about what a rule may be: `allowed_results` must include
`unknown` (a rule that cannot report uncertainty will manufacture certainty
instead), `inference_allowed: true` needs a written justification, and an
unimplemented rule needs a stated reason. The runner then refuses any verdict
with no evidence, and any `fail`, `unknown` or `not_applicable` with no
rationale.

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
| `list_tables()` | profiled delimited tables and workbook worksheets |
| `read_rows(path, columns, offset, limit)` | bounded source observations with row locators and integrity proof |
| `filter_rows(path, filters, columns, limit)` | bounded deterministic selection with a closed operator registry |
| `aggregate(path, metrics, group_by, filters)` | complete-scan count/missing/sum/mean/min/max summaries |
| `describe_variable(path, column)` | profile + deterministic observed summary for one column |
| `join_tables(left, right, left_keys, right_keys, ...)` | explicit-key join with cardinality diagnostics |
| `list_relationships(status?)` | saved declared/deterministic/candidate/rejected table relationships |
| `get_relationship(id)` | one evidence-bearing relationship record |
| `join_relationship(id, ...)` | execute only a saved declared/deterministic join contract |
| `get_metadata(path)` | recognised metadata files, served verbatim |
| `get_evidence(...)` | what supports a claim |
| `get_provenance()` | when, how long, with what version this was ingested |
| `resolve_identifier(value)` | where an identifier occurs (no network call) |

In the `fair-*` modes only:

| Tool | Returns |
| --- | --- |
| `list_fair_rules()` | the canonical rule registry |
| `get_fair_indicator(rule_id)` | one rule in full, exactly as authored |
| `run_fair_check(rule_id?)` | a deterministic, evidence-bound assessment |
| `validate_identifier(value)` | syntax against the scheme; no network call |

Resources: `dataset://manifest`, `dataset://metadata`, `dataset://provenance`,
`dataset://evidence`, `dataset://relationships`, `dataset://files/{path}`.

Timestamps live in `provenance.json` rather than the manifest, so that repeated
ingests of identical bytes still compare byte-for-byte — but they are surfaced
by `dataset_inventory()`, `get_provenance()` and the report header. A fact
nobody can reach is as good as absent.

Every tool re-checksums a file before returning its content, and withholds it on
a mismatch. An answer drawn from drifted bytes is worse than no answer, because
it is indistinguishable from a good one.

## Cross-table relationships

Relationship resolution is a derived step and never rewrites the ingest
manifest. Run:

```bash
data2agent relationships ./preclinical-agent
```

to write `relationships.json`. Structural discovery is deliberately
conservative: a shared subject-identifier-shaped column with overlapping values
is recorded as `candidate`, never promoted to fact.

Explicit declarations can be supplied as JSON:

```bash
data2agent relationships ./preclinical-agent \
  --declarations relationships.declared.json
```

A declaration records the left/right tables and key columns, including composite
keys. It can optionally state an expected cardinality. If the observed
cardinality disagrees, or no complete key overlaps, the relationship is stored
as `rejected`.

Saved relationship records include backing-file checksums, completeness,
uniqueness, observed cardinality, overlap counts and example source-row
locators. `join_relationship(id)` refuses `candidate` and `rejected`
records; only `declared` or future convention-proven `deterministic`
relationships can drive a named join.

Until relationship resolution has actually run,
`relationships_determined: false` remains the API answer.

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
| `fair-rules` | + the canonical rules, which it must apply itself | v0.2 |
| `fair-deterministic` | + the checks, run by code — the reference condition | v0.2 |
| `fair-skill` | `structured` + FAIR prose | planned |
| `fair-semantic` | + vocabularies and SHACL | planned |

Each FAIR mode is a superset of the one before it, so the ladder varies the
*form* of the constraint while the information underneath stays identical.
`structured` exposes no FAIR concept at all — a test asserts it, and a layering
check fails the build if the core ever imports a profile.

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
| [`docs/fair-profile-contract.md`](docs/fair-profile-contract.md) | the canonical FAIR rule format, the 12 rules, the projections |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | v0.1 → v0.6 |
| [`docs/BACKLOG.md`](docs/BACKLOG.md) | numbered work items with acceptance criteria |

## Development

```bash
pip install -e '.[mcp,fair,dev]'
pytest          # 139 tests
ruff check src tests && ruff format --check src tests
```

Tests are grouped by layer — `tests/ingestion/`, `tests/evidence/`,
`tests/mcp/`, `tests/fair/` — and the acceptance criteria map onto them
one-to-one in [`docs/ROADMAP.md`](docs/ROADMAP.md). `tests/test_layering.py`
asserts the dependency arrows still point one way.

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
