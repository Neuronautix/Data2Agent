# Data2MCP tools

All tools are read-only. None of them writes to the dataset, and each
re-checksums a file before returning any of its content.

## `dataset_inventory()`

Start here. Returns identity, file count, total bytes, format counts, recognised
metadata files, identifier count, warnings and skipped paths.

Fields worth reading carefully:

- `dataset_id` — pins the exact bytes. Quote it when reporting results.
- `warnings` — unrecognised formats, ragged rows, resolved sentinel tokens,
  unresolved ambiguous tokens, decode
  failures. An empty list does not mean the dataset is clean; it means nothing
  was flagged by the checks that exist.
- `relationships_determined: false` — relationship resolution has not been
  run. The empty `relationships` list is not a finding. After
  `data2agent relationships <output>`, saved relationship records are surfaced
  with explicit epistemic status.

## `list_files(pattern=None, file_format=None)`

`pattern` is a glob over dataset-relative paths (`"*.csv"`, `"sub-*/**"`).
`file_format` filters on the detected format id (`csv`, `json`, `markdown`,
`unknown`, …). Both are optional and combine.

## `inspect_file(path, preview_bytes=4096)`

Manifest record plus integrity status plus a bounded UTF-8 preview.

- `preview_truncated: true` means you are seeing the head of the file only.
- `preview_encoding: "binary"` means the file is not valid UTF-8; no textual
  preview is offered and none should be imagined.
- If `integrity.matches` is `false`, the preview is withheld and
  `preview_withheld` explains why. Re-ingest; do not work around it.

## `inspect_table(path)`

For profiled delimited tables and workbook worksheets. A worksheet is addressed
as `<workbook>#<sheet>`. This tool returns the **recorded structural profile**,
not the source observations: rows, columns, token shapes, missingness and
warnings. Use `read_rows` when the actual values are needed.

Per column:

| Field | Meaning |
| --- | --- |
| `dtype` | `empty`/`integer`/`number`/`boolean`/`string` — the shape of the observed tokens, **not** the scientific type. Cells resolved to missing contribute no type |
| `values` | cells holding a value under the active convention |
| `missing` | `missing_empty + missing_sentinel`. Read it with `missing_value_convention` |
| `missing_empty` | cells that are empty |
| `missing_sentinel` | cells holding a token the active convention resolves to missing |
| `sentinel_tokens_seen` | the exact tokens resolved, as written, with counts |
| `ambiguous_tokens_seen` | `unknown`, `-`, `?` and friends. **Not** missing — counted as values, reported for a human to rule on |
| `distinct` | distinct values, **an upper bound** when `distinct_exact` is `false` |
| `distinct_values` | present only when the column is below the enumeration cap |

`missing_convention` on the table repeats the convention in force, so a table
profile is readable on its own. A count without its convention is not
reproducible by anyone who does not share your assumptions — always quote both.

`ragged_rows` counts rows whose field count differs from the header's. Rows are
never padded to fit.

## `list_tables()`

Lists every profiled delimited table and workbook worksheet with its backing
file, row count, column names, profiling state and warnings. Start here when a
dataset contains multiple worksheets rather than guessing sheet names.

## `read_rows(path, columns=None, offset=0, limit=100)`

Returns actual source observations through a bounded, read-only query.

- Maximum rows per call: 1000. Larger requested limits are reported and capped.
- The backing file is re-checksummed before any value is served.
- `columns` is an explicit projection; omit it to return every profiled column.
- `offset` is a zero-based data-row offset after the profiled header.
- Each returned record carries `source_row`.
- For worksheets, `source_row` is the real 1-based Excel row.
- For CSV/TSV, it is the physical line on which the logical CSV record ends.
- Empty cells and declared missing sentinels become `null`. A sentinel's
  original token is retained in that row's `missing` map.
- Ambiguous tokens such as `unknown`, `-` and `?` remain values.
- Numeric/boolean coercion follows the dtype already established at ingest; the
  query does not infer a new type.

This is observation access, not semantic interpretation or analysis. A column
named `dose` is still only a column until metadata/semantics establishes what
it means.

## `filter_rows(path, filters, columns=None, limit=100)`

Select observations with a closed operator registry: `eq`, `ne`, `lt`,
`lte`, `gt`, `gte`, `in`, `not_in`, `contains`, `is_missing`,
and `is_not_missing`.

Each filter is an object such as:

```json
{"column": "genotype", "op": "eq", "value": "KO"}
```

No Python, SQL, regex or free-form expression is executed. At most 100,000
source rows are scanned. If a larger table is only partially scanned, the
response says `scan_complete: false` and `truncated: true`; a partial search
must never be reported as an exhaustive negative finding.

## `aggregate(path, metrics, group_by=None, filters=None)`

Compute deterministic summaries over a **complete** scan. Supported metrics are
`count`, `n_present`, `n_missing`, `n_distinct`, `sum`, `mean`, `min`, `max`,
`median`, `sd` (sample, n − 1) and `sem` (sd / √n). The arithmetic ones require
a column profiled as integer or number. Every response carries
`metric_definitions` for the metrics it used, and every null metric carries a
reason in `null_reasons` (`sd`/`sem` are null below two values, never 0).

Full signature:
`aggregate(path, metrics, group_by=None, filters=None, unit=None, unit_metrics=None, on_inconsistent_unit="refuse", unit_sample=50)`.

Example:

```json
{
  "path": "animals.csv",
  "group_by": ["genotype"],
  "metrics": [
    {"op": "count", "name": "n"},
    {"op": "mean", "column": "weight_g", "name": "mean_weight_g"}
  ]
}
```

Numeric aggregation uses decimal arithmetic internally. A table above the
100,000-row complete-scan cap is refused rather than summarized partially.

### Repeated measures: declare the unit of analysis

If a table has several rows per animal (time bins, trials, sessions), a plain
`aggregate` averages rows: an animal with more bins weighs more, and `count` is
the number of rows, not animals. Declare the experimental unit instead:

```json
{
  "path": "behaviour.csv",
  "group_by": ["genotype", "treatment"],
  "unit": ["animal_id", "day"],
  "unit_metrics": [{"op": "sum", "column": "dig_dur_s", "name": "dig_total"}],
  "metrics": [
    {"op": "mean", "column": "dig_total"},
    {"op": "sem", "column": "dig_total"},
    {"op": "count", "name": "n_animals"}
  ]
}
```

Stage 1 reduces rows to one record per unit with `unit_metrics`; stage 2
summarises units per group with `metrics`, whose columns must name
`unit_metrics` outputs (default name `op:column`, e.g. `sum:dig_dur_s`). Each
group reports `n_units`, `n_rows`, `unit_metric_missing`, and up to
`unit_sample` contributing units with their stage-1 values and source-row
locators. The choice of reduction (sum, mean, …) is a scientific decision you
make and should state; it is echoed back in `operation`.

If one unit's rows disagree on a `group_by` value (an animal under two
genotypes), the result is refused (`analysis_unit.status: "refused"`) and the
conflicting units are listed with row locators. `on_inconsistent_unit="exclude"`
computes over the consistent units only and reports how many were excluded.
Rows with a missing unit key are counted and located, never attributed.

## `aggregate_join(metrics, relationship_id=None, left=None, right=None, left_keys=None, right_keys=None, how="inner", ...)`

The same aggregation (including `unit`) over the **complete** result of a join,
for when the grouping variable lives in another table. Name the join by a
declared `relationship_id`, or give `left`, `right`, `left_keys`, `right_keys`.
Every column reference is qualified `left.<column>` or `right.<column>`:

```json
{
  "relationship_id": "rel-…",
  "group_by": ["right.genotype"],
  "unit": ["left.animal_id", "left.day"],
  "unit_metrics": [{"op": "sum", "column": "left.dig_dur_s", "name": "dig_total"}],
  "metrics": [{"op": "mean", "column": "dig_total"}, {"op": "count"}]
}
```

Both files are re-checksummed and cited under `inputs`; `join` reports the
cardinality, diagnostics, joined row count and warnings. Many-to-many joins are
refused (they multiply rows), as is a join above the complete-aggregation cap.
In a one-to-many join, a row-level metric over the unique side's columns is
warned about, because each of its values is repeated once per match.

## `describe_variable(path, column)`

Returns the ingest-time column profile together with a deterministic runtime
summary. For numeric columns this includes count, missing count, min, max and
mean. This describes observed tokens/values only; it does not infer units,
biological meaning, treatment roles or ontology terms.

## `join_tables(left, right, left_keys, right_keys, ...)`

Join two tables only on keys explicitly supplied by the caller. No relationship
is inferred or written back to the manifest.

The response reports `one_to_one`, `one_to_many`, `many_to_one`, or
`many_to_many` cardinality plus duplicate-key counts. Missing/null keys never
match each other. Many-to-many multiplication is explicitly warned about.
Supported join types are `inner` and `left`.

Both inputs must be completely readable under the 50,000-row-per-side join
safety cap. Output is capped at 1,000 rows, with `total_result_rows` and
`truncated` stating whether more matches exist.

## Relationship tools

### `list_relationships(status=None)`

Lists the saved `relationships.json` bundle. Status is one of:

- `declared` — explicitly stated in the declaration configuration.
- `deterministic` — reserved for a supported metadata/convention rule that
  proves the relationship without model inference.
- `candidate` — structural overlap worth inspecting, but not established.
- `rejected` — an explicit declaration whose observed data violate the
  required relationship, for example wrong cardinality or zero key overlap.

Every record carries left/right tables, possibly composite keys, backing-file
checksums, key completeness and uniqueness, cardinality, overlap coverage and
example source-row locators. There is no confidence score: confidence must not
blur the distinction between candidate and declared facts.

If no relationship sidecar exists, the tool returns
`determined: false`. If backing source bytes have drifted since resolution,
saved assertions are withheld.

### `get_relationship(relationship_id)`

Returns one saved evidence-bearing relationship by stable ID.

### `join_relationship(relationship_id, ...)`

Executes the saved keys through the deterministic join engine. Only
`declared` and `deterministic` relationships are executable. A candidate can
be inspected, but it cannot silently become an analysis join.

Relationship resolution itself is intentionally **not an MCP write tool**. Use
the CLI outside the agent session:

```bash
data2agent relationships <output>
data2agent relationships <output> --declarations declarations.json
```

This keeps the MCP surface read-only and makes declarations reviewable input
artifacts.

## `get_metadata(path=None)`

With no argument: lists files whose *names* match a published convention
(RO-Crate, Frictionless, BIDS, ISA-Tab, CodeMeta, DataCite, README, LICENSE).

With a path: returns that file's content **verbatim**. Quote from it rather than
paraphrasing — a paraphrase cannot be checked against the bytes.

Recognition is by filename only. It does not mean the file is valid, complete,
or even parseable.

## `get_evidence(claim_id=…, subject=…, check=…, contains=…, limit=100)`

The tool to call before asserting anything.

- `subject` — a dataset-relative path, or the literal `"dataset"`.
- `check` — a check id, e.g. `table.missing-value-count`.
- `contains` — a case-insensitive substring of the claim text.
- `claim_id` — fetch one claim directly.

`total` is the number of matches; `returned` is how many came back under
`limit`. If `total` exceeds `returned`, narrow the query rather than assuming
you have seen everything.

## `get_provenance()`

When the ingest ran, how long it took, which tool version produced it, and
whether the source was verified unchanged.

These facts are deliberately **not** in the manifest: a manifest carrying a clock
reading could never be compared for equality, and byte-identical repeat ingest is
the property the whole system leans on. `dataset_inventory` also surfaces
`ingested_at`, `ingest_duration_s` and `tool_version` for convenience.

Note the distinction this tool does *not* cover: `provenance.json` is the
provenance of the **ingest run**, not of the dataset. The dataset's own
provenance — who produced it, where it came from — lives in its metadata, and
the FAIR rule `R1.2-PROVENANCE-DECLARED` is what assesses that.

## `resolve_identifier(value)`

Reports **where** an identifier occurs in the dataset. `resolved` is always
`null` in this version: no network call is made and none is implied. A hit is
not evidence that the identifier is valid or resolvable.

## Resources

| URI | Content |
| --- | --- |
| `dataset://manifest` | the full manifest |
| `dataset://provenance` | run details: time, host, tool version |
| `dataset://evidence` | the full claim ledger, including the check registry |
| `dataset://metadata` | recognised metadata files |
| `dataset://relationships` | saved relationship bundle or explicit not-determined state |
| `dataset://files/<path>` | one file's record, integrity and preview |

Resources are gated by mode just as tools are. In `raw` only
`dataset://files/<path>` is served; the others raise rather than being served
outside their condition.

## FAIR tools (`fair-*` modes only)

### `list_fair_rules()` and `get_fair_indicator(rule_id)`

The canonical rule registry, and one rule in full. Read the rule's `question`
and `notes` before interpreting any result: the notes say what the rule does
**not** cover, which is usually more than you would assume.

### `run_fair_check(rule_id=None)`

A deterministic assessment, in the shape of `assessment.schema.json`. Each result
carries `result`, `evidence` (claim ids and inline records) and, for anything but
a plain pass, a `rationale` naming what was looked at.

Traps:

- **`unknown` is a result.** Two rules need the network or the dataset's
  published location and always return unknown. Report them as such.
- **`not_applicable` is not `fail`.** It means the rule does not apply here, and
  the rationale says why.
- **A `pass` is narrow.** It means the rule's literal check held, nothing wider.
- **`unsupported_claims: []`** on a deterministic run is by construction, not a
  finding. It is the slot a *model-generated* assessment gets scored in.

### `validate_identifier(value)`

Syntax against the scheme — including the ORCID check digit. Returns
`resolves: null` and `resolution_attempted: false`, every time. A syntactically
valid identifier is not a resolvable one.

## Modes

The server is started in a mode that gates which tools exist:

- `raw` — `list_files`, `inspect_file` only. The benchmark control condition.
- `structured` — the full deterministic surface above. The default. **No FAIR
  tools, deliberately.**
- `fair-rules` — adds the rule registry. You can read every rule, but you must
  apply it yourself.
- `fair-deterministic` — adds `run_fair_check` and `validate_identifier`. The
  reference condition.
- `fair-skill`, `fair-semantic` — specified but **not implemented**; requesting
  one fails rather than falling back, so a run is never mislabelled.

If a tool you expect is absent, check the mode before assuming a fault. If you
are in `structured` and asked for a FAIR verdict, say the assessment needs a
`fair-*` mode — do not produce one yourself.
