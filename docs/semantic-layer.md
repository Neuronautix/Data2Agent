# Optional semantic layer (runtime ontology constraints)

Status: **experimental, disabled by default.** Nothing here runs unless you call
`data2agent semantic` or start `serve` with `--semantic-snapshot` and
`--semantic-policy`. The `fair-semantic` benchmark mode stays *not
implemented*: enabling a benchmark mode needs a maintainer decision (AGENTS.md),
and that mode also promises `validate_shacl` (D2A-85/87), which this layer does
not provide.

This is a first, narrow step on epic #2 (D2A-80, D2A-82, D2A-83, part of D2A-87).
It adds no runtime dependency: the engine and the record builder are standard
library only.

## Architecture

```text
ingest (deterministic) -> manifest.json + evidence.json        unchanged, never written here
                                   |
                     data2agent.semantic (opt-in)               reads them; re-hashes any source bytes it quotes
                                   |
             semantic/semantic-mapping.json                     sidecar bound to dataset_id, manifest and ledger hashes
```

The layering test enforces the arrows: `ingest` and `evidence` never import
`semantic`; `semantic` never imports `profiles` or `mcp`.

## Inputs (all versioned, all pinned)

| File | Contract | What it fixes |
| --- | --- | --- |
| ontology snapshot | `d2a-ontology-snapshot-1` | the authorised terms: canonical IRI, label, synonyms, definition, kind, parents, domain/range, deprecation; the SHA-256, origin and status of every source artifact; identified by the SHA-256 of its canonical JSON |
| build spec | `d2a-snapshot-build-1` | how a snapshot is compiled from pinned artifacts (`python -m data2agent.semantic.compile spec.json --out snapshot.json [--check]`; RDF artifacts need the `semantic-build` extra) |
| mapping policy | `d2a-mapping-policy-1` | per field: whole ontologies, branches and explicit term sets allowed, exclusions, datatype, term kind, cardinality; `closed_world: true` is required; bound to one snapshot by SHA-256 |
| bindings | `d2a-semantic-bindings-1` | which source field (`{table, column}` or `{file, key}`) fills which policy field: a user declaration, recorded by hash, never inferred |
| inferred (optional) | `d2a-semantic-inferred-1` | decisions of an external selector (a model, an expert, SSIC B3) with the selector's identity |

A policy exists because OWL alone does not say which terms a field may take: OWL
is open-world and describes what a term means, not field admissibility.

## The four operations (`data2agent.semantic.constraints`)

| Operation | Returns |
| --- | --- |
| `get_mapping_constraints(task_profile)` | per field: datatype, kinds, cardinality, declared scope and the resolved admissible IRIs |
| `search_ontology_terms(query, constraints, top_k)` | candidates inside the field's scope only (IRI, CURIE, label, synonyms, definition, ontology, version, score, match reason); `out_of_scope_matches` counts what was withheld; an empty list is a valid answer |
| `get_term_context(iri)` | parents, ancestors, children, domain/range, relations, deprecation and replacement, ontology version, the fields that admit it |
| `validate_semantic_mapping(mapping, constraints)` | per decision: `admissible`, `invalid` (codes `unknown_iri`, `label_not_iri`, `outside_scope`, `excluded`, `deprecated_term`, `kind_mismatch`, `cardinality_violation`, `datatype_mismatch`, `literal_not_allowed`, `missing_reason`, `value_with_abstention`) or `unresolved` (an abstention or `needs_review` with a reason, which is valid) |

Ranking is fixed: exact label 1.0, exact synonym 0.95, otherwise 0.8 x token
Jaccard over label and synonyms plus 0.15 x the share of query tokens found in
the definition; ties break by IRI. CURIEs are expanded with the snapshot's
declared prefixes and reported (`non_canonical_form`); a bare label is refused.

**Formal validity is not scientific correctness.** Every validation result
carries that note. A mapping can be admissible and wrong.

A missing, unreadable or re-hashed snapshot raises `VocabularyUnavailable`;
callers turn it into `needs_review` (`unavailable_outcome`), never into a
fallback vocabulary or a live lookup.

## The record

```bash
data2agent ingest examples/preclinical-minimal -o out
data2agent semantic out --snapshot examples/semantic/snapshot.json \
  --policy examples/semantic/policy.json --bindings examples/semantic/bindings.preclinical-minimal.json \
  [--inferred examples/semantic/inferred.example.json]
```

`semantic/semantic-mapping.json` (`d2a-semantic-mapping-1`) lists, per binding,
the source fact (raw values, ledger claim ids, the checksum they were read under)
and, per value, exactly one status:

- `verified_source_fact`: the raw value as the source holds it;
- `deterministic_transformation`: the value equals exactly one authorised
  term's label or synonym (or is in a literal value set); a lookup, not a judgment;
- `inferred_mapping`: an imported selector decision that passed validation,
  with the selector named;
- `unresolved`: everything else, with the reason and the candidates
  considered (listed, never committed).

No fuzzy match is ever promoted (BACKLOG, D2A-82). The record has no timestamp,
so it is byte-identical across runs. A JSON source key is read only after its
file re-hashes to the manifest's checksum. An inferred decision about a value the
source does not hold is listed under `rejected_inferences`.

In the example, `Species: Mus musculus` resolves deterministically to
NCBITaxon:10090, `License: CC0-1.0` is in the literal set, and the `sex` codes
`M`/`F` stay unresolved: nothing in the dataset defines them, so they become
mappings only as labelled inferences.

## Agent-facing tools

`serve --semantic-snapshot S --semantic-policy P` adds
`get_mapping_constraints`, `search_ontology_terms`, `get_term_context` and
`validate_semantic_mapping` to whatever mode is served, and
`get_semantic_mapping` when a record bound to that dataset, manifest, snapshot
and policy exists. Without the flags every mode's tool list is unchanged (tested).

## Reuse elsewhere

`constraints.py` and `compile.py` import nothing else from `data2agent` and
are vendored byte-identically by the SSIC pilot in fair-hcm-repository, which
consumes Data2Agent only through a pinned CLI. Digests at the time of writing:

- `src/data2agent/semantic/constraints.py`: `752f9a0a95f93b220c605944f3405b956228e55ad8367f900dc9e9d8791b9b3e`
- `src/data2agent/semantic/compile.py`: `89d645a7f00363801df22c1df7481cb39c0707af85c4c0b76b802e2eb7819d0a`

## Limitations

- The example snapshot is a **hand-transcribed extract** of NCBITaxon and PATO
  for tests, not a release. Verify IRIs and labels against the official releases
  before any research use.
- No SHACL engine is bundled; `validate_semantic_mapping` accepts a SHACL
  callable and otherwise reports `shacl: not_run`.
- Table values are mapped only when the manifest holds the column's exhaustive
  distinct values; otherwise the source fact itself is `unresolved`.
- Nothing here has been evaluated on real data; see Data2AgentBench and the SSIC
  B3 protocol for the experimental design.
