# Evidence contract

## The invariant

```text
claim
  ↓
evidence
  ↓
source file
  ↓
immutable checksum
```

Every statement Data2Agent emits must be reducible to a **named deterministic
check** run over **named bytes**. There is no "probably" tier and no unsourced
tier. A statement that cannot be reduced this way is not a statement Data2Agent
is permitted to make.

This is not a style preference. It is what makes *unsupported-claim rate* an
automatically computable metric rather than a matter of reviewer opinion — and
that metric is the dependent variable of the whole research programme.

## Record shape

```json
{
  "claim_id": "clm_9f2c4b1a77e0d35c",
  "claim": "'sex' is missing for 12 of 48 row(s) in 'animals.csv'",
  "subject": "animals.csv",
  "evidence": [
    {
      "source": "animals.csv",
      "source_sha256": "3b1f…",
      "check": "table.missing-value-count",
      "result": 12,
      "field": "sex",
      "locator": "column:sex"
    }
  ]
}
```

- `claim_id` is **content-addressed**: `clm_` + the first 16 hex characters of a
  SHA-256 over the canonicalised claim. The same dataset yields the same ids on
  every machine, so two runs' ledgers can be diffed directly.
- `source_sha256` pins the exact bytes the check ran on. If the file later
  changes, the mismatch is detectable without re-running anything.
- `locator` says *where inside* the source: `column:sex`, `line:12`.

## Enforcement

Two rules are enforced in code, in `EvidenceLedger.record`:

1. **A claim with no evidence is rejected.** `ValueError`, at write time.
2. **A check id not in the registry is rejected.** `ValueError`, at write time.

The registry (`data2agent.evidence.model.CHECKS`) maps each check id to a
human-readable definition of what it computes, and is embedded in every
`evidence.json`. A fact can only enter the record through a check that somebody
named and implemented — adding one is a visible, reviewable act.

## The v0.1 check registry

| Check id | Computes |
| --- | --- |
| `file.checksum` | SHA-256 over the complete file bytes |
| `file.size` | Size in bytes |
| `file.format-detection` | Format from magic bytes and/or extension |
| `dataset.file-count` | Files inventoried under the root |
| `dataset.identity` | The SHA-256 fold over sorted `(path, checksum)` pairs |
| `convention.missing-values` | The named missing-value convention applied, and its source |
| `table.row-count` | Data rows after the header |
| `table.column-list` | Header names, in order |
| `table.column-dtype` | Least upper bound of observed token shapes |
| `table.missing-value-count` | Rows whose cell is empty **or** holds a resolved token |
| `table.missing-empty-count` | Rows whose cell is empty |
| `table.missing-sentinel-count` | Cells holding a token the convention resolves to missing |
| `table.ambiguous-token-count` | Cells holding a token that no convention resolves |
| `json.shape` | Top-level type, keys, nesting depth |
| `metadata.file-convention` | Filename matches a published convention |
| `identifier.detected` | A persistent-identifier pattern matched in text |

### Missingness claims carry their rule

A missing count is only meaningful alongside the rule that produced it, so every
`table.missing-value-count` claim cites four evidence items: the total, its two
components, and the `convention.missing-values` record that resolved the tokens.

```json
{
  "claim": "'strain' is missing for 3 of 48 row(s) in 'animals.csv' (0 empty, 3 resolved from tokens by the 'default-sentinels' convention)",
  "evidence": [
    { "check": "table.missing-value-count",    "result": 3 },
    { "check": "table.missing-empty-count",    "result": 0 },
    { "check": "table.missing-sentinel-count", "result": 3 },
    { "check": "convention.missing-values",
      "result": { "id": "default-sentinels", "source": "built-in default (...)",
                  "tokens": ["na", "n/a", "null", "..."],
                  "ambiguous_tokens_resolved": false } }
  ]
}
```

A reader who disagrees with the convention can see precisely what to change, and
re-ingesting under a different one produces a different, equally sourced number.
`tests/evidence/test_ledger.py` asserts that no missingness claim can be recorded
without its convention.

## How an agent is expected to use it

1. Call `dataset_inventory` to orient.
2. Call `inspect_table` / `get_metadata` for specifics.
3. **Before asserting anything, call `get_evidence`.**
4. If no claim supports the statement, report it as unknown. Do not fill the gap.

`get_evidence` accepts `claim_id`, `subject`, `check`, or a `contains`
substring, so "what do we actually know about `animals.csv`?" is one call.

## Extending the ledger

When a later version adds a check — a FAIR indicator, a SHACL validation, a
curation edit — it must:

1. Register the check id and its definition in `CHECKS`.
2. Emit evidence citing the file(s) and checksum(s) the check consumed.
3. Keep `unknown` available as a result. A check that cannot return "unknown" is
   a check that will eventually invent an answer.

A profile assessment (`schemas/assessment.schema.json`) cites claim ids from
`evidence.json` rather than restating their content, so a FAIR verdict is always
traceable to bytes without duplicating them. The runner enforces the same
discipline one level up: it refuses a verdict the rule did not permit, a verdict
with no evidence, and a `fail`, `unknown` or `not_applicable` with no rationale.

## What evidence does *not* do

It does not make a claim *true* — only *sourced*. `table.row-count` can be
sourced and still wrong if the delimiter was misread. That is why the profile
layer keeps `unknown` as a first-class result, and why format detection records
`detected_by` so a reader can see whether the bytes or merely the filename were
consulted.
