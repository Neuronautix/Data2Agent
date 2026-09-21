# Evidence discipline

## The invariant

```text
claim → evidence → source file → immutable checksum
```

Every statement must reduce to a named deterministic check over named bytes.
There is no "probably" tier. If nothing supports a statement, the statement does
not get made.

## Before you assert anything

1. Call `get_evidence` with the `subject` (the file path) or a `contains`
   substring.
2. If a claim supports your statement, cite its `claim_id`.
3. If none does, **say the thing is not determined**. Do not reason your way to
   a value from context, convention, or the rest of the dataset.

## What counts as evidence

A claim record looks like this:

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

`source_sha256` pins the exact bytes. `check` names what was computed —
`evidence.json` carries the full check registry, so you can always look up what
a check id means.

## Traps

**A plausible value is still an invented value.** If 36 of 48 animals are male
and 12 have no recorded sex, the 12 are *unknown*. Not "likely male", not
"probably the same cohort".

**`NA` is a token, not a missing value.** It is counted in
`null_like_tokens`, separately from `missing`, because nothing in most datasets
defines what `NA` means there. Do not merge the two counts, and do not report
"15 missing" when the manifest says 12 missing and 3 null-like.

**A detected identifier is a pattern match.** `resolve_identifier` reports
occurrences. It does not resolve anything, and finding a DOI-shaped string is
not evidence that the DOI exists.

**A column's `dtype` is a token shape, not a meaning.** `birth_date` reads as
`string` because nothing declares a date format. Reporting it as "a date column"
adds an interpretation the bytes do not carry.

**`detected_by: "extension"` means the filename was believed.** Say so when it
matters; a `.csv` that is really a PNG is a real failure mode, and the manifest
reports the byte signature when the two disagree.

**Withheld content is not missing content.** If `integrity.matches` is `false`,
the file changed since ingest. Report the drift and re-ingest. Do not work from
a cached earlier value.

**An empty list means "not determined".** `relationships: []` is not "this
dataset has no relationships". `dataset_inventory` returns
`relationships_determined: false` so you never have to guess which it is.

## The phrasing that is always available

> The dataset does not record this. `<tool>` reports `<fact>`; nothing in the
> manifest or evidence ledger establishes `<the thing asked about>`.

That is a complete answer. It is more useful than a confident one that turns out
to be invented, and a reader can act on it.
