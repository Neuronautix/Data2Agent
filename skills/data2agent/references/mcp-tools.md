# Data2MCP tools

All tools are read-only. None of them writes to the dataset, and each
re-checksums a file before returning any of its content.

## `dataset_inventory()`

Start here. Returns identity, file count, total bytes, format counts, recognised
metadata files, identifier count, warnings and skipped paths.

Fields worth reading carefully:

- `dataset_id` — pins the exact bytes. Quote it when reporting results.
- `warnings` — unrecognised formats, ragged rows, null-like tokens, decode
  failures. An empty list does not mean the dataset is clean; it means nothing
  was flagged by the checks that exist.
- `relationships_determined: false` — this version does not compute cross-file
  relationships. The empty `relationships` list is not a finding.

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

For `csv` / `tsv` files that were successfully profiled. Raises if the file was
not profiled as a table — that is a real signal (the delimiter could not be
established), not an error to route around.

Per column:

| Field | Meaning |
| --- | --- |
| `dtype` | `empty`/`integer`/`number`/`boolean`/`string` — the shape of the observed tokens, **not** the scientific type |
| `non_empty` | cells with content |
| `missing` | cells that are **empty**. Nothing else counts here |
| `null_like_tokens` | cells holding `NA`, `null`, `unknown`, `.`, `-`, `?` — whether these mean "missing" is undetermined |
| `distinct` | distinct values, **an upper bound** when `distinct_exact` is `false` |
| `distinct_values` | present only when the column is below the enumeration cap |

`ragged_rows` counts rows whose field count differs from the header's. Rows are
never padded to fit.

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
| `dataset://files/<path>` | one file's record, integrity and preview |

## Modes

The server is started in a mode that gates which tools exist:

- `raw` — `list_files`, `inspect_file` only. The benchmark control condition.
- `structured` — the full surface above. The default.
- `fair-*` — specified but **not implemented**; requesting one fails rather than
  falling back, so a run is never mislabelled.

If a tool you expect is absent, check the mode before assuming a fault.
