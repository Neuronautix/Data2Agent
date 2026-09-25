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

layout:                      # optional; see "Table headers" below
  declaration: ./layouts.json  # data2agent ingest SRC -o OUT --layout layouts.json

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
├── relationships.json  optional derived cross-table relationships
├── mcp/
│   ├── server.json   host-agnostic server definition
│   └── USAGE.md      how to connect it from any MCP host
└── report/
    └── dataset-report.md   every statement carries a claim id
```

Schemas: `schemas/dataset-manifest.schema.json`, `schemas/evidence.schema.json`,
`schemas/relationships.schema.json`, and `schemas/assessment.schema.json`.

`relationships.json` is not produced by ingest. It appears only after
`data2agent relationships <output>`, keeping relationship assessment separate
from the byte-stable ingest contract. It records the `manifest_sha256` it was
computed against; after any re-ingest that changes `manifest.json` (for example
switching `--strict-missing` on or off) the service refuses it until
relationships are regenerated.

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
| `format: "ole2-container"` | OLE2 bytes whose directory holds no BIFF workbook stream | a legacy `.xls` |
| `reader.cell_values: "cached"` | a formula cell was read as its last computed value | the value was typed, or recomputed |
| `warnings: []` | nothing flagged | the dataset is clean |
| `skipped: [...]` | present in the directory, absent from the manifest | ignorable |
| `header_source: "first-non-empty"` | the first non-blank row was used, as before D2A-97 | the header was verified |
| `header_detection.confident: false` | the rule could not establish a header and fell back | the header is wrong |
| `header_detection.skipped_rows: []` | nothing before the data was skipped | there was no title |
| `layout_declaration: null` | no layout was declared | every header is right |

### Workbook formats

A workbook's format is established from its bytes: OOXML and XLSB by the
package's part names, ODS by its `mimetype` member, legacy BIFF `.xls` by a
`Workbook` (BIFF8) or `Book` (BIFF5) stream in the OLE2 directory. The name
only ever produces an `extension_conflict` when it disagrees.

| Format | Optional extra | Backend (`reader.backend`) |
| --- | --- | --- |
| `xlsx` | `xlsx` | `openpyxl` |
| `xls`, `xlsb`, `ods` | `workbooks` | `calamine` |

Every backend produces the same sheet profile for the same sheet: blank cells
are empty, whole numbers are integers, dates are midnight datetimes, and
`source_row` is the spreadsheet's own row number. The backend's version is
run-specific and lives in `provenance.configuration.workbook_readers`, not in
the manifest. Without the extra, the workbook is reported as unprofiled with a
`workbook.reader-unavailable` claim naming the extra, never as a dataset with
no tables. Merged ranges are not reported by openpyxl's streaming mode or for
XLSB/ODS, so `merged_ranges: 0` is not evidence of none.

### Table headers

Every column name rests on one choice: which row is the header. Lab files rarely
put it on the first line. A registry sheet carries a banner ("Weights" over
the columns it groups) above the real header; a scoring export writes
`Subjects:` and `Behaviors:` rows before its column names; a summary sheet
stacks a treatment row over a repeated metric row. Taking the first non-empty
row as the header in such files names every column after the banner, and every
join on `ID` or `Genotype` then fails with "unknown column".

The header is therefore chosen by an explicit rule, `d2a-header/1`
(`src/data2agent/ingest/layout.py`), identical for worksheets and delimited
files. Among the first 16 non-blank rows, a row is **header-like** when it has
at least two non-blank cells, is not a `key:` label row, is text (a row that is
at least three-quarters text also qualifies if it is strictly more textual than
the row below it, so a header may name dose columns `0.02`, `0.07`), and has
more than half as many non-blank cells as the widest of it and the five rows
after it. A row is **preamble** to a header when it has at most half the
header's non-blank cells (`sparse`) or its first cell is text ending in `:`
(`key-value-label`). The header is the first header-like row, provided every
non-blank row above it is preamble; the search stops at the first header-like
row either way.

| Outcome | `header_source` | `confident` | Warning |
| --- | --- | --- | --- |
| the first non-blank row is header-like | `first-non-empty` | `true` | none |
| a later row is, and everything above it is preamble | `detected` | `true` | names the rows skipped |
| nothing qualifies, or a non-preamble row sits above the first header-like row | `first-non-empty` | `false` | only when the first row itself looks like preamble |
| a layout declaration names the table | `declared` | `true` | none |

Detection never composes a multi-row header: a sparse row directly above the
header may be a group label or a title, and nothing structural separates the
two. When a skipped row sits directly above the header, the warning names the
declaration that would keep its labels.

Each table profile, delimited or worksheet, carries:

```json
"header_row": 3,
"header_rows": 1,
"data_starts_row": 4,
"header_source": "detected",
"header_detection": {
  "rule": "d2a-header/1",
  "confident": true,
  "detected_header_row": 3,
  "skipped_rows": [{"row": 2, "reason": "sparse", "non_empty_cells": 10}]
}
```

Row numbers are what a person would cite: the spreadsheet row, or for a CSV/TSV
the physical line on which the record starts. Row readers (`read_rows`,
`filter_rows`, `aggregate`, joins, relationship assessment) start at
`data_starts_row`, and `source_row` stays the file's own row or line number.
The decision is also a claim in the evidence ledger, under the check
`table.header-layout`.

#### Declaring a layout

A declaration overrides detection for the tables it names:

```json
{
  "layout_version": "1",
  "layouts": {
    "registry.xlsx#Sheet1": {"header_row": 2, "header_rows": 2},
    "exports/scoring.tsv": {"header_row": 2, "header_rows": 2, "upper_label_fill": "none"},
    "trace.csv": {"header_row": 2, "data_starts_row": 4, "note": "line 3 holds units"}
  }
}
```

| Key | Meaning |
| --- | --- |
| table path | exactly as `manifest.tables` keys it: `<file>` or `<workbook>#<sheet>` |
| `header_row` | required; 1-based row (or line) of the first header row |
| `header_rows` | how many rows the header spans (default 1, at most 10) |
| `data_starts_row` | where observations begin (default: the row after the header); rows skipped between are recorded as `declared` |
| `upper_label_fill` | `forward` (default) or `none`; see below |
| `note` | free text, kept in the manifest |

A multi-row header is composed column by column, top row first, joining the
non-blank labels with ` / `: a `PBS` row over a `Score` row gives `PBS / Score`.
With `upper_label_fill: forward`, a blank cell in an *upper* row takes the label
to its left, which is how a label merged or centred across a group of columns
reads; the fill stops where a row above starts a new label and where nothing
below names the column, and the lowest row is never filled. Each column of a
multi-row header records its cells as written in `header_cells`, so every fill
is auditable. Duplicate composed names are disambiguated exactly as single-row
names are.

Validation is strict: an unknown key, a non-positive or boolean row number, a
`data_starts_row` inside the header, a `header_row` beyond the table, or a
table path that matches no profiled table stops the ingest with an error before
anything is written. A declaration that silently applied to nothing would leave
someone believing a header was fixed when it was not.

The declaration's SHA-256 is recorded twice. `manifest.layout_declaration`
(`{"sha256", "tables"}`) makes it part of how the dataset was read: re-ingesting
the same bytes under another declaration yields another `manifest.json`, so a
`relationships.json` bound to the old one is refused until regenerated.
`provenance.configuration.layout_declaration` adds the file name, which is
run-specific. Tables the declaration names show `header_source: "declared"`,
with the declaration under `header_detection.declared` and what detection would
have chosen still in `detected_header_row`.

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

## Relationship artifact semantics

The manifest continues to carry `relationships: []` as **not determined by
ingest**. Relationship resolution is an explicit derived step.

Without `relationships.json`, the service reports
`relationships_determined: false`. After resolution it overlays the derived
bundle in `dataset_inventory()` without rewriting the manifest.

Relationship status is epistemic, not a confidence score:

| Status | Meaning |
| --- | --- |
| `candidate` | structural overlap observed; author intent is not established |
| `declared` | explicitly supplied in a declaration configuration and consistent with the observed key facts |
| `deterministic` | reserved for a supported metadata/convention rule that establishes the link without model inference |
| `rejected` | an explicit declaration conflicts with observed overlap/cardinality |

Every record contains table/key endpoints, backing checksums, complete and
incomplete key-row counts, uniqueness, observed cardinality, overlap coverage,
and bounded source-row examples. Composite keys are arrays and are never
collapsed into concatenated strings.

A declaration can state `expected_cardinality`; disagreement makes the record
`rejected`. Candidate and rejected records cannot drive
`join_relationship()`.

Saved relationship assertions are withheld if their backing source files no
longer match the manifest checksums.

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
