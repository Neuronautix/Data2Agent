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
| D2A-04 | Delimited-table profiling: schema, row counts, missingness, null-like tokens | done |
| D2A-05 | JSON shape profiling | done |
| D2A-06 | Metadata-file recognition by published naming convention | done |
| D2A-07 | Persistent-identifier detection with source and line | done |
| D2A-08 | Evidence ledger with an enforced check registry | done |
| D2A-09 | `manifest.json` / `provenance.json` / `evidence.json` split | done |
| D2A-10 | Generic Data2MCP: six tools + `dataset://` resources | done |
| D2A-11 | Host-agnostic service separated from the MCP binding | done |
| D2A-12 | Benchmark mode gating, with loud failure on unimplemented modes | done |
| D2A-13 | Published JSON Schemas for manifest, evidence and assessment | done |
| D2A-14 | Worked example dataset + acceptance tests (75 tests) | done |
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

## v0.2 — FAIR as a separable profile

| id | Item | Acceptance |
| --- | --- | --- |
| D2A-20 | `profiles/fair/profile.yaml` + rule registry in YAML | Every rule validates against a published rule schema |
| D2A-21 | Rule schema (`schemas/fair-rule.schema.json`) | Requires `id`, `principle`, `question`, `check`, `allowed_results`, `inference_allowed` |
| D2A-22 | Registry covers F, A, I and R with at least one rule each | Rule ids stable and referenced by every mode |
| D2A-23 | Deterministic check implementations for identifier presence, licence presence, metadata-schema presence, vocabulary reference presence | Each returns one of `pass`/`fail`/`unknown`/`not_applicable` with evidence |
| D2A-24 | MCP tools `run_fair_check`, `get_fair_indicator`, `validate_identifier`, `validate_metadata_schema` | Exposed only in `fair-*` modes |
| D2A-25 | Enable modes `fair-skill`, `fair-rules`, `fair-deterministic` | `data2agent modes` reports them available; mode-gating tests updated |
| D2A-26 | Rule → Markdown Skill projection (for `fair-skill`) | Generated from the canonical rule, never hand-written |
| D2A-27 | Emit `assessment.json` against the existing schema | Schema validation in CI |
| D2A-28 | `validate_identifier` records its network attempt in provenance | Endpoint, status and timestamp stored; the only non-reproducible check |
| D2A-29 | Leak test: `--mode structured` exposes no FAIR concept | Assert the tool list and every response body are FAIR-free |

D2A-29 is the one that protects the experiment. Without it, FAIR vocabulary
drifts into the core and the control condition quietly stops being a control.

---

## v0.3 — curation, export and remote sources

| id | Item | Acceptance |
| --- | --- | --- |
| D2A-30 | Accept a DOI, repository URL or archive as `dataset.source` | Remote source snapshotted locally before any other step; `dataset_id` computed from the snapshot |
| D2A-31 | Curated export with a new `dataset_id` | The export never overwrites the source |
| D2A-32 | Every modification carries provenance | Each change records what, why, by whom, from which prior `dataset_id` |
| D2A-33 | Round-trip test: curated export re-ingests cleanly | Manifest of the export is itself deterministic |
| D2A-34 | Diff two manifests | Reports added/removed/changed files and changed profiles |
| D2A-35 | YAML metadata parsing | Requires a parser dependency — keep it optional so the core stays stdlib-only |
| D2A-36 | Excel/`.xlsx` table profiling | Optional dependency; same profile shape as CSV |
| D2A-37 | Exact distinct counts above the enumeration cap | Currently reported as an upper bound with `distinct_exact: false` |

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
| D2A-72 | Keep `unknown` a first-class result everywhere | A layer that cannot say "unknown" will invent an answer |
| D2A-73 | Manifest-version discipline | Any change to manifest bytes for unchanged input is a major version bump |
| D2A-74 | Performance on large datasets | Currently checksums everything twice (once to hash, once to verify). Acceptable at MVP scale; revisit past ~10 GB |
| D2A-75 | Symlink policy | Recorded, never followed. Revisit only with a concrete dataset that needs it |
| D2A-76 | Security review of served content | The service withholds drifted content; it does not yet sandbox previews of hostile files |

---

## Explicitly not doing

| Not doing | Why |
| --- | --- |
| Importing Paper2AgentBench into this repository | Upstream keeps its benchmark separate, and that is the right pattern. Data2AgentBench stays its own repository |
| A generic everything-converter | One real preclinical dataset, done properly, first |
| Hard-coding FAIR into Data2MCP | Destroys the control condition the study depends on |
| An LLM anywhere in the ingest path | An LLM that infers a dataset's structure is an LLM that can invent one |
| Building a FAIR ontology before the rule registry | Encodes guesses about which questions matter, before knowing |
| Treating `NA` as missing by default | A dataset-specific convention. Counted separately until something in the dataset documents it |
