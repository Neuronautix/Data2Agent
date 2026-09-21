# Data contract

What Data2Agent accepts, what it produces, and what each field is allowed to mean.

## Input contract

```yaml
dataset:
  source: ./dataset          # a directory (v0.1)
                             # DOI / repository URL / archive: v0.3, see BACKLOG.md

metadata:                    # discovered, never required
  optional:
    - README.md
    - metadata.json
    - dataset_description.json
    - ISA-Tab files
    - RO-Crate
    - repository metadata

output:
  directory: ./dataset-agent # must sit OUTSIDE the dataset source
```

Rules:

1. **The source is read-only.** Ingestion never writes to it, and re-checksums
   every file afterwards to prove it (`provenance.source_verified_unchanged`).
2. **The output may not live inside the source.** Writing a manifest into the
   dataset would change the thing the manifest describes. This is refused with
   an error, not a warning.
3. **No metadata file is required.** A dataset that has none is ingested
   normally; the absence is reported, never filled in.

## Dataset identity

```text
dataset → inventory → checksums → immutable dataset identity
```

`dataset_id` is a SHA-256 fold over the sorted `(relative_path, sha256)` pairs:

```text
dataset_id = sha256( concat over sorted paths of: path "\n" sha256 "\n" )
```

Properties, all covered by `tests/ingestion/test_determinism.py`:

- Independent of filesystem walk order, machine, clock and absolute path.
- Changes if any byte changes, if a file is renamed, added or removed.
- Two agent configurations are only comparable at equal `dataset_id`. The
  benchmark depends on this more than on anything else in the system.

## Output contract

```text
<output>/
├── manifest.json     what the dataset IS        (deterministic, no timestamps)
├── provenance.json   what this RUN was          (timestamps, host, tool version)
├── evidence.json     claim → evidence → bytes   (deterministic)
├── mcp/
│   ├── server.json   host-agnostic server definition
│   └── USAGE.md      how to connect it from any MCP host
└── report/
    └── dataset-report.md   every statement carries a claim id
```

Schemas: `schemas/dataset-manifest.schema.json`, `schemas/evidence.schema.json`,
and `schemas/assessment.schema.json` (declared in v0.1, produced from v0.2).

## Manifest field semantics

The subtle part is what an *empty* or *absent* value means. It is never "none".

| Field | Means | Does **not** mean |
| --- | --- | --- |
| `relationships: []` | not determined by this version | the dataset has no relationships |
| `metadata_files: []` | no filename matched a known convention | the dataset has no metadata |
| `identifiers: []` | no pattern matched in scanned text | the dataset has no identifiers |
| `column.dtype` | shape of the observed tokens | the column's scientific type |
| `column.missing` | count of **empty** cells | count of unknown values |
| `column.null_like_tokens` | count of tokens like `NA` | count of missing values |
| `column.distinct` + `distinct_exact: false` | an upper bound | the exact cardinality |
| `format: "unknown"` | no signature or extension matched | the file is corrupt |
| `detected_by: "extension"` | the *name* said so, the bytes did not | verified format |
| `warnings: []` | nothing flagged | the dataset is clean |

### Column shape vocabulary

`dtype` is the least upper bound of the token shapes observed in the column:

| dtype | Every non-empty cell matched |
| --- | --- |
| `empty` | (no non-empty cells at all) |
| `integer` | `^[+-]?\d+$` |
| `number` | integer or decimal or scientific notation |
| `boolean` | `true` / `false`, case-insensitive |
| `string` | anything else, **or** a mixture of the above |

A column of ISO dates is `string`. That is correct and deliberate: nothing in
the dataset declares a date format, so parsing one out would be an inference.

## Missingness, precisely

Three distinct states, never merged:

| Cell content | Counted as | Rationale |
| --- | --- | --- |
| `""` (empty after strip) | `missing` | the file records no value |
| `NA`, `n/a`, `null`, `unknown`, `.`, `-`, `?` | `null_like_tokens` | a *value* whose meaning the dataset does not define |
| anything else | `non_empty` | a value |

If a dataset documents its own sentinel convention, a future profile rule can
promote its null-like tokens to missing **with that documentation as evidence**.
Until then the distinction stands.

## Integrity at serve time

The MCP service re-checksums a file before returning any of its content. On a
mismatch it returns the integrity status and withholds the content:

```json
{
  "integrity": { "matches": false, "expected_sha256": "…", "observed_sha256": "…" },
  "preview": null,
  "preview_withheld": "the file no longer matches its manifest checksum; re-ingest before relying on it"
}
```

An answer drawn from drifted bytes is worse than no answer, because it is
indistinguishable from a good one.

## Versioning

`manifest_version` and `evidence_version` are independent of the package
version. A change that alters the bytes of a manifest for unchanged input is a
**major** manifest version bump, because it invalidates stored comparisons.
Adding a new optional field is a minor bump.
