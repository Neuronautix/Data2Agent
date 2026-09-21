# Architecture

## The one-sentence version

> Take an immutable scientific dataset and expose it through a reproducible,
> evidence-preserving MCP interface.

Everything else — FAIR assessment, curation, semantic validation, multi-agent
workflows — is a layer *on top of* that, and is separable from it by design.

## Layer diagram

```text
            hosts:  Claude Code · Codex · Goose · Pi · OpenCode · a test
                                    │
                    ════════ standard MCP boundary ════════
                                    │
                              Data2MCP  (src/data2agent/mcp/)
                       server.py  ← thin binding, no behaviour
                       service.py ← all behaviour, host-agnostic
                       modes.py   ← benchmark tool gating
                                    │
                    ┌───────────────┴───────────────┐
                    │                               │
              evidence layer                  profiles (v0.2+)
        (src/data2agent/evidence/)          fair/ · domain/ · …
        claim → evidence → file → sha256
                    │
              deterministic ingest  (src/data2agent/ingest/)
        inventory · checksum · formats · tabular · structured
        · identifiers · metadata · provenance · pipeline
                    │
              immutable dataset  (read-only, never written to)
```

Arrows point downward only. Nothing in `ingest/` knows about MCP; nothing in
`evidence/` knows about FAIR; nothing below the MCP boundary knows which host
started it. Those three facts are what make the benchmark in
`benchmark-contract.md` possible at all.

## Why ingestion has no language model

Paper2Agent needs agents to *discover* how arbitrary research software works,
because that information exists only in tutorials, examples and runtime
behaviour. A dataset is different: its structure is a property of its bytes.
Row counts, column headers, missingness, checksums and format signatures are
all computable exactly, and an exact answer is strictly better than a
confident-sounding one.

So `src/data2agent/ingest/` is:

- **Deterministic** — the same bytes always give the same manifest, byte for byte.
- **Dependency-free** — stdlib only, so it still runs in ten years.
- **Read-only** — the source is re-checksummed after every run to prove it.
- **Non-interpretive** — it reports shapes, never meanings.

An agent enters the picture *after* this, to reason over a structure it did not
have to guess at.

## Why one generic MCP server

Paper2Agent generates paper-specific MCP servers because every research
repository exposes different functionality. Datasets are not like that: the
useful operations (*what files are here, what shape is this table, what does the
metadata say, what supports that claim*) are the same across datasets. A single
fixed server is therefore testable, comparable across datasets, and free of the
"did the generator have a bad day" variance that would otherwise contaminate
every benchmark result.

The generic server is also the thing that makes `mode: raw` a meaningful
control: the only difference between conditions is what the agent is handed, not
which code was generated for it.

## Why manifest and provenance are separate documents

The obvious design puts `ingested_at` next to `dataset_id` in one file. We
don't, because "repeated ingest gives the same manifest" is the property
everything else leans on, and a manifest carrying a clock reading can never be
compared for equality.

| Document | Contains | Stable across runs? |
| --- | --- | --- |
| `manifest.json` | what the dataset **is** | yes, byte-identical |
| `provenance.json` | what this **run** was (time, host, tool version) | no, by definition |
| `evidence.json` | every claim, and what supports it | yes, byte-identical |

They are joined by `dataset_id`.

## Why the service and the binding are separate modules

`service.py` imports nothing from `mcp` and nothing host-specific. `server.py`
only translates. If host-specific behaviour ever accumulates below the MCP
boundary, the benchmark stops measuring hosts and starts measuring our
accommodations of them — so the separation is a correctness requirement, not
tidiness.

A practical consequence: every tool is directly unit-testable without a protocol
in the loop, and a future non-MCP harness needs no rewrite.

## Unknown stays unknown

The rule that constrains every layer:

> Never infer a value that the dataset does not state.

Not animal sex, not strain, not acquisition device, not experimental condition,
not units — however plausible. Concretely, in v0.1:

- An empty cell is **missing**, and is counted.
- A literal `NA` token is **a token**, counted separately, because no dataset in
  this example states what `NA` means. Folding the two together would be an
  inference dressed as arithmetic.
- `relationships: []` means *not determined*, and the API says so explicitly via
  `relationships_determined: false`.
- A detected DOI is a **pattern match**, never a claim that it resolves.
- A `distinct` count above the enumeration cap is labelled `distinct_exact: false`.
- An unrecognised format is `unknown`, with a warning — never a guess.

`tests/evidence/test_no_invented_metadata.py` asserts this negatively, by
demanding that plausible-but-unstated terms are absent from the output.

## Repository layout

```text
Data2Agent/
├── skills/
│   ├── data2agent/          portable skill for any skill-capable host
│   └── paper2agent/         upstream skill, retained for the v0.5 port
├── src/data2agent/
│   ├── ingest/              deterministic scanning
│   ├── evidence/            claim → evidence → file → checksum
│   ├── mcp/                 service (behaviour) + server (binding) + modes
│   ├── report.py            evidence-backed Markdown + host connection files
│   └── cli.py
├── schemas/                 the published output contracts
├── examples/                worked datasets
├── tests/                   acceptance tests, grouped by layer
└── docs/
```

Directories named in the long-term plan but absent today (`profiles/fair/`,
`validation/`, `export/`) are absent on purpose: an empty package that promises
behaviour is worse than a backlog entry that schedules it. See
`BACKLOG.md`.
