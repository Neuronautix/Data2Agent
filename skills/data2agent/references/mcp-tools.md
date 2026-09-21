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
| `dataset://files/<path>` | one file's record, integrity and preview |

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
