# Backlog

Work items for Data2Agent, grouped by the release that should carry them. Each
has an id (`D2A-nn`), an acceptance criterion, and — where the decision is not
obvious — the reasoning behind it.

**Status key:** `done` · `open` · `blocked` · `deferred`

Ordering principle, from `ROADMAP.md`: stabilise one layer before the next
depends on it.

---

## v0.1 — deterministic core

| id | Item | Status |
| --- | --- | --- |
| D2A-01 | Repository fork with Paper2Agent lineage preserved (`NOTICE.md`, MIT notice retained in `LICENSE`) | done |
| D2A-02 | Deterministic inventory + checksums + `dataset_id` fold | done |
| D2A-03 | Format detection by magic bytes and extension; unknown stays unknown | done |
| D2A-04 | Delimited-table profiling: schema, row counts, missingness | done |
| D2A-05 | JSON shape profiling | done |
| D2A-06 | Metadata-file recognition by published naming convention | done |
| D2A-07 | Persistent-identifier detection with source and line | done |
| D2A-08 | Evidence ledger with an enforced check registry | done |
| D2A-09 | `manifest.json` / `provenance.json` / `evidence.json` split | done |
| D2A-10 | Generic Data2MCP: six tools + `dataset://` resources | done |
| D2A-11 | Host-agnostic service separated from the MCP binding | done |
| D2A-12 | Benchmark mode gating, with loud failure on unimplemented modes | done |
| D2A-13 | Published JSON Schemas for manifest, evidence and assessment | done |
| D2A-14 | Worked example dataset + acceptance tests | done |
| D2A-15 | **Manual two-host check: connect the generated server from both Claude Code and Codex** | open |
| D2A-16 | CI: tests, lint, determinism re-check, stdlib-only guard | done |
| D2A-17 | Replace the synthetic example with one real preclinical dataset | open |

### D2A-15 — two-host verification

`tests/mcp/test_server_binding.py` exercises the protocol surface in-process:
tool registration, mode gating, tool calls, resource reads. It does **not** prove
a real host can start and drive the server. That needs a human to run
`mcp/USAGE.md` under Claude Code and under Codex and record the result.

Until it is done, the v0.1 acceptance list is 7/8, not 8/8. Worth stating
plainly rather than letting an in-process test stand in for the claim.

### D2A-17 — a real dataset

`examples/preclinical-minimal` is synthetic, so the tests can assert exact
numbers. A real preclinical dataset will break assumptions the synthetic one
cannot: inconsistent encodings, mixed delimiters, filename-encoded metadata,
Excel exports with merged headers, units in free text. Each break is a backlog
item, and finding them early is the point.

Do one real dataset properly rather than building a generic
everything-converter.

---

## v0.2 — conventions, timestamps, FAIR · shipped

### Missing-value conventions

| id | Item | Status |
| --- | --- | --- |
| D2A-18 | Named missing-value conventions; `NA` resolves to missing under a declared rule | done |
| D2A-19a | `missing` / `missing_empty` / `missing_sentinel` breakdown, plus the tokens as written | done |
| D2A-19b | Ambiguous tokens (`unknown`, `-`, `?`) counted, never resolved | done |
| D2A-19c | Frictionless `missingValues` read from `datapackage.json` when present | done |
| D2A-19d | `--missing-tokens` / `--strict-missing` overrides | done |
| D2A-19e | Every missingness claim cites the convention that produced it | done |
| D2A-19j | UTF-8 BOM stripped before parsing, encoding reported as `utf-8-sig` | done |

### Timestamps

| id | Item | Status |
| --- | --- | --- |
| D2A-19f | `duration_seconds` recorded in provenance | done |
| D2A-19g | `ingested_at` surfaced via `dataset_inventory()` and the report header | done |
| D2A-19h | `get_provenance()` tool + `dataset://provenance` resource | done |
| D2A-19i | Manifest stays timestamp-free; byte-identical repeat ingest preserved | done |

### FAIR profile

| id | Item | Status |
| --- | --- | --- |
| D2A-20 | `profiles/fair/profile.yaml` + rule registry in YAML | done |
| D2A-21 | Rule schema (`schemas/fair-rule.schema.json`), validated in CI | done |
| D2A-22 | Registry covers F, A, I and R — 12 rules | done |
| D2A-23 | Deterministic check implementations | done — 10 of 12 |
| D2A-24 | MCP tools `list_fair_rules`, `get_fair_indicator`, `run_fair_check`, `validate_identifier` | done |
| D2A-25 | Enable modes `fair-rules`, `fair-deterministic` | done |
| D2A-27 | Emit `assessment.json` against the published schema | done |
| D2A-29 | Leak test: `--mode structured` exposes no FAIR concept | done |
| D2A-29b | Layering test: the core may not import a profile | done |
| D2A-29c | Modes gate resources as well as tools; `raw` serves no manifest or ledger | done |
| D2A-26 | Rule → Markdown Skill projection (for `fair-skill`) | open — v0.3 |
| D2A-28 | `validate_identifier` resolves over the network, recording the attempt | open — v0.3 |
| D2A-24b | `validate_metadata_schema` against a declared schema | open — v0.3 |

D2A-29 and D2A-29b are the ones that protect the experiment. Without them, FAIR
vocabulary drifts into the core and the control condition quietly stops being a
control.

### D2A-26 — the prose projection

`fair-skill` needs Markdown generated *from* the canonical rules, not written
alongside them. Hand-writing the prose would confound the form of a constraint
with its content and make prose-vs-rules uninterpretable — the whole reason the
registry is canonical in the first place.

### D2A-28 — network resolution

`F1-PID-RESOLVABLE` and `A1-RETRIEVAL-PROTOCOL` return `unknown` today because
neither can be settled from a local snapshot. When resolution arrives it must
record endpoint, status and timestamp in `provenance.json`: it is the only check
in the system that is not reproducible from the dataset bytes alone, and that
has to be visible rather than assumed.

---

## v0.3 — prose projection, curation, export, remote sources

| id | Item | Acceptance |
| --- | --- | --- |
| D2A-26 | Generate the `fair-skill` Markdown from the canonical rules | Three FAIR modes live; prose-vs-rules-vs-deterministic runnable |
| D2A-30 | Accept a DOI, repository URL or archive as `dataset.source` | Remote source snapshotted locally before any other step; `dataset_id` computed from the snapshot |
| D2A-31 | Curated export with a new `dataset_id` | The export never overwrites the source |
| D2A-32 | Every modification carries provenance | Each change records what, why, by whom, from which prior `dataset_id` |
| D2A-33 | Round-trip test: curated export re-ingests cleanly | Manifest of the export is itself deterministic |
| D2A-34 | Diff two manifests | Reports added/removed/changed files and changed profiles |
| D2A-35 | YAML metadata parsing | PyYAML is already an optional `fair` extra; the ingest core must stay stdlib-only |
| D2A-36 | Excel/`.xlsx` table profiling | Optional dependency; same profile shape as CSV |
| D2A-37 | Exact distinct counts above the enumeration cap | Currently reported as an upper bound with `distinct_exact: false` |
| D2A-38 | Per-column missing-value conventions | Frictionless allows a per-resource declaration; we apply one convention per dataset |
| D2A-39 | Promote an ambiguous token on documented evidence | A dataset that documents `unknown` in prose should be able to resolve it, with that text as evidence |

---

## v0.4 — semantics

| id | Item | Acceptance |
| --- | --- | --- |
| D2A-40 | Controlled vocabulary registry | Terms resolvable offline from a pinned snapshot |
| D2A-41 | `validate_vocabulary` MCP tool | Unmapped terms return `unknown`, never a nearest match |
| D2A-42 | RDF parsing (Turtle, JSON-LD, RDF/XML) | Detection exists today; parsing does not |
| D2A-43 | SHACL shapes generated from the canonical rules | Shapes are a projection, not a reimplementation |
| D2A-44 | `validate_shacl` MCP tool; enable `fair-semantic` | Violations carry the shape id and the offending triple |
| D2A-45 | Ontology mappings for one domain (preclinical) | Mappings are evidence-bearing and reversible |

D2A-41 matters more than it looks: fuzzy-matching an unmapped term to its
nearest vocabulary entry is inference wearing a lookup's clothes.

---

## v0.5 — multi-agent workflow

| id | Item | Acceptance |
| --- | --- | --- |
| D2A-50 | Coordinator skill, ported from `skills/paper2agent/` | Runs on any host with subagent spawning |
| D2A-51 | Inventory agent, FAIR assessor, domain metadata specialist, curation agent | Each has an explicit input and output contract |
| D2A-52 | Independent verifier over the evidence ledger | Verifier sees the claims, not the assessor's reasoning |
| D2A-53 | Orchestrator comparison: one agent vs. assessor+verifier vs. specialist team | Same `dataset_id`, same mode, differing only in orchestration |
| D2A-54 | Cost and latency recorded per orchestration | Feeds the benchmark's cost metric |

Deliberately last. Starting here means debugging ingestion, MCP, FAIR logic and
orchestration at once, with no reference implementation to compare against.

---

## v0.6 — benchmark harness

| id | Item | Acceptance |
| --- | --- | --- |
| D2A-60 | Harness adapters for Claude Code, Codex, Goose, Pi | Live in **Data2AgentBench**, not here |
| D2A-61 | Run matrix: harness × orchestrator × model × constraint | Every cell records a complete `generator` block |
| D2A-62 | Unsupported-claim-rate scorer | Computed automatically from the evidence ledger |
| D2A-63 | Unknown-preservation-rate scorer | Counts `unknown` results kept as `unknown` |
| D2A-64 | Fabrication scorer | Flags asserted values absent from the source bytes |
| D2A-65 | Reproducibility check: same cell, same seed, same result | Deviations reported, not averaged away |

---

## Cross-cutting

| id | Item | Notes |
| --- | --- | --- |
| D2A-70 | Keep the ingest core dependency-free | Every proposed dependency goes in an optional extra. Ten-year reproducibility is the requirement |
| D2A-71 | Keep the MCP boundary host-agnostic | No host-specific behaviour below `server.py`, ever |
| D2A-72 | Keep `unknown` a first-class result everywhere | A layer that cannot say "unknown" will invent an answer. The loader now refuses a rule whose `allowed_results` omit it |
| D2A-73 | Manifest-version discipline | Any change to manifest bytes for unchanged input is a major version bump |
| D2A-74 | Performance on large datasets | Currently checksums everything twice (once to hash, once to verify). Acceptable at MVP scale; revisit past ~10 GB |
| D2A-75 | Symlink policy | Recorded in `skipped` with a warning, never followed — `is_symlink()` is tested before `is_file()`, which follows links. Revisit only with a concrete dataset that needs internal links (BIDS derivatives do) |
| D2A-76 | Security review of served content | The service withholds drifted content and bounds preview reads; it does not yet sandbox previews of hostile files |
| D2A-77 | Partial/interrupted output directories | The service refuses one whose manifest, provenance and evidence disagree on `dataset_id`; ingest does not yet write atomically, so a half-written directory is still possible |

---

## Explicitly not doing

| Not doing | Why |
| --- | --- |
| Importing Paper2AgentBench into this repository | Upstream keeps its benchmark separate, and that is the right pattern. Data2AgentBench stays its own repository |
| A generic everything-converter | One real preclinical dataset, done properly, first |
| Hard-coding FAIR into Data2MCP | Destroys the control condition the study depends on |
| An LLM anywhere in the ingest path | An LLM that infers a dataset's structure is an LLM that can invent one |
| Building a FAIR ontology before the rule registry | Encodes guesses about which questions matter, before knowing |
| Treating `NA` as missing **silently** | Resolving it is fine, and is now the default. Resolving it without naming the convention, storing it in the manifest and citing it in every claim is not: the number stops being reproducible by anyone who does not share our assumptions |
| Resolving `unknown`, `-` or `?` by default | Not standard sentinels. A cell reading `unknown` may be a considered statement; a dataset that means it as missing can declare it |
