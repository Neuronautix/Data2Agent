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
    - datapackage.json       # if it declares missingValues, that convention is used
    - ISA-Tab files
    - RO-Crate
    - repository metadata

missing_values:              # optional; see "Missingness, precisely" below
  convention: default-sentinels | strict-empty-only | <explicit token list>

output:
  directory: ./dataset-agent # must sit OUTSIDE the dataset source
```

Rules:

1. **The source is read-only.** Ingestion never writes to it. Afterwards it
   re-walks the tree and re-checksums every file, so a file that appeared or
   disappeared mid-run counts as drift exactly as a changed byte does
   (`provenance.source_verified_unchanged`).
2. **The output may not live inside the source.** Writing a manifest into the
   dataset would change the thing the manifest describes. This is refused with
   an error, not a warning.
3. **No metadata file is required.** A dataset that has none is ingested
   normally; the absence is reported, never filled in.
4. **Symlinks are never followed**, to a file or to a directory. Following one
   would put bytes from outside the dataset into a manifest that claims to
   describe it. They appear in `skipped` and raise a warning, so the omission is
   visible rather than silent.
5. **A UTF-8 byte-order mark is stripped**, and the encoding is reported as
   `utf-8-sig`. A BOM decodes cleanly as UTF-8, so without an explicit check it
   survives into the first column name and every claim about that column —
   and spreadsheet exports carry one routinely.

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
| `metadata_files: []` | nothing was recognised, by filename convention **or** by content structure | the dataset has no metadata |
| `metadata_candidates: [...]` | a recogniser applied to these files and could not read them | they hold metadata |
| `identifiers: []` | no pattern matched in scanned text | the dataset has no identifiers |
| `column.dtype` | shape of the observed tokens | the column's scientific type |
| `column.missing` | empty cells **plus** tokens the active convention resolves | a count anyone would reproduce without knowing the convention |
| `column.missing_sentinel` | tokens resolved to missing, under a named rule | tokens we judged to look empty |
| `column.ambiguous_tokens_seen` | tokens like `unknown` that no built-in convention resolves | missing values |
| `column.distinct` + `distinct_exact: false` | an upper bound | the exact cardinality |
| `format: "unknown"` | no signature or extension matched | the file is corrupt |
| `detected_by: "extension"` | the *name* said so, the bytes did not | verified format |
| `warnings: []` | nothing flagged | the dataset is clean |
| `skipped: [...]` | present in the directory, absent from the manifest | ignorable |

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

## Metadata recognition

A file is recognised as metadata by one of two rules, and every entry records
which one fired and what the other one said.

| Rule | `recognised_by` | What it matches |
| --- | --- | --- |
| Filename convention | `filename_convention` | `README.md`, `datapackage.json`, `dataset_description.json`, `ro-crate-metadata.json`, ISA-Tab files, … |
| Content structure | `content` | a JSON object that names a published standard in its own content (`@context` on a known vocabulary, `BIDSVersion`, a Frictionless `resources` list, a DataCite record shape); a table with exactly one complete, unique subject-identifier column where every other column takes at most half as many distinct values as there are rows |

Both claims are structural. Neither says what the file *means*: a recognised
registry table is reported as "one row per distinct `<column>`, with N grouping
columns", never as "these are the animals in the experiment". Interpreting it is
the semantic layer's job.

`kind` separates the two things a recognised file can be:

- `document` — the file's content *is* the metadata record. It is not a data file.
- `embedded` — metadata carried inside a file that is also payload, such as a
  registry worksheet in a workbook of results. It stays a data file, so a wrong
  recognition can add a metadata finding but can never silently subtract a data
  file from the format and linkage checks.

`path` is the file, or `<workbook path>#<sheet name>` when the metadata is one
worksheet; `file` always names the file whose bytes carry it.

A file a recogniser applied to and could not finish reading — a workbook with no
reader installed, an unparseable JSON document — is listed in
`metadata_candidates`, never in `metadata_files`. That keeps "we looked and found
nothing" distinguishable from "we could not look", and a dataset with candidates
and no recognitions makes `F2-METADATA-PRESENT` report `unknown` rather than
`fail`.

The rule is deliberately narrow, because five FAIR indicators cascade from
`metadata_files` and a data table wrongly called metadata corrupts all five at
once. Metadata in a shape neither rule covers — a PowerPoint deck, a lab
notebook, a descriptor that declares no standard — is not counted. That is a
false negative, and it is the direction this contract chooses to be wrong in.

## Missingness, precisely

A blank cell records no value. A cell holding `NA` records a *token*, and whether
that token means "missing" is a property of the dataset's conventions rather than
of its bytes. Data2Agent resolves such tokens through an explicit, **named**
convention: the convention is data, it is stored in the manifest, and every
missingness claim cites it.

That is what keeps "NA means missing" a declared rule rather than a judgement
call. Change the convention and the numbers change, visibly, with the reason
attached.

### The three classes

| Cell content | Class | Counted in |
| --- | --- | --- |
| `""` (empty after strip) | absent | `missing_empty` → `missing` |
| a token the **active convention** names | sentinel | `missing_sentinel` → `missing` |
| `unknown`, `-`, `--`, `?`, `.`, `not recorded`, `no data`, `n.a.`, `tbd` | ambiguous | `ambiguous_tokens_seen`, and `values` |
| anything else | value | `values` |

Always: `missing == missing_empty + missing_sentinel`, and
`rows == values + missing`.

A cell resolved to missing contributes no type and no distinct value, so
`weight_g` holding `18, NA, 22` is an `integer` column with one missing value —
not a `string` column, which is what a naive reading would produce.

### The conventions

| Convention | `id` | Tokens resolved |
| --- | --- | --- |
| Declared by the dataset | `declared` | whatever a Frictionless `missingValues` declares |
| Built-in default | `default-sentinels` | `na`, `n/a`, `#n/a`, `#n/a n/a`, `#na`, `nan`, `-nan`, `<na>`, `null`, `none`, `nil` |
| Strict | `strict-empty-only` | none — only an empty cell is missing |
| Explicit override | `custom` | whatever `--missing-tokens` names |

Precedence: an explicit override beats the dataset's declaration, which beats
the built-in default. Whichever applied is recorded in
`manifest.missing_value_convention`, with its `source` in plain words.

The default set is aligned with the sentinel list pandas has used for years —
the closest thing to a cross-tool default convention that exists — and is
deliberately narrower than "anything that looks empty".

### Why ambiguous tokens are never resolved

`unknown`, `-` and `?` are not standard sentinels, and a cell reading `unknown`
may be a considered statement rather than an absence. They are counted and
reported so a human can rule on them, and a dataset that means them as missing
can say so:

```bash
data2agent ingest ./dataset -o ./out --missing-tokens unknown,NA
```

An undeclared sentinel is a genuine reusability defect — a reader cannot tell
"not measured" from "measured as zero" from a strain literally named `NA`, and
every downstream tool will guess differently. The FAIR profile reports exactly
that as `R1.3-MISSING-VALUES-DECLARED`.

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

## Timestamps

`manifest.json` carries no timestamp, no absolute path and no host details, so
that repeated ingests of identical bytes compare byte-for-byte. Everything
run-specific lives in `provenance.json`:

```json
{
  "started_at": "2026-09-21T08:09:18Z",
  "finished_at": "2026-09-21T08:09:18Z",
  "duration_seconds": 0.003,
  "tool": { "name": "data2agent", "version": "0.1.0" },
  "source_verified_unchanged": true
}
```

Keeping them out of the manifest is not the same as hiding them. A fact nobody
can reach is as good as absent, so the ingest timestamp is surfaced by:

- `dataset_inventory()` — as `ingested_at`, `ingest_duration_s`, `tool_version`;
- `get_provenance()` — the full run record;
- `dataset://provenance` — the same, as a resource;
- `report/dataset-report.md` — under "This ingest run".

## Versioning

`manifest_version` and `evidence_version` are independent of the package
version. A change that alters the bytes of a manifest for unchanged input is a
**major** manifest version bump, because it invalidates stored comparisons.
Adding a new optional field is a minor bump.
