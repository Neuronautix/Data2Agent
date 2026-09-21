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

**`NA` counts as missing — under a named convention, which you must quote.**
The manifest records which convention resolved it and where that convention came
from. "3 missing" is only reproducible by someone who knows the rule, so report
it as "3 missing (0 empty, 3 resolved from `NA` under `default-sentinels`)".
Every missingness claim in the ledger cites the convention; carry that citation
through.

**`unknown`, `-` and `?` are not resolved by any built-in convention.** They
appear in `ambiguous_tokens_seen`, and they count as *values*, not as missing. A
cell reading `unknown` may be a deliberate statement. Do not fold them into the
missing count on your own judgement; if the dataset documents them as missing,
the fix is to re-ingest with `--missing-tokens`, and to say that you did.

**A convention is a reading, not a fact about the bytes.** `dataset_id` does not
change when the convention changes. Two reports with the same `dataset_id` and
different conventions are describing the same bytes differently — quote both.

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

**A FAIR `unknown` is a result, not a gap for you to fill.** Two rules cannot be
run from a local snapshot. Report them as unknown with the reason the assessment
gives. Reasoning about how likely a DOI is to resolve is the exact failure this
system is built to prevent.

**A FAIR `pass` is narrow.** It means the rule's literal check held. Call
`get_fair_indicator` for the rule's own question and `notes`, which state what
it does not cover, and do not promote a `pass` into "this dataset is findable".

## The phrasing that is always available

> The dataset does not record this. `<tool>` reports `<fact>`; nothing in the
> manifest or evidence ledger establishes `<the thing asked about>`.

That is a complete answer. It is more useful than a confident one that turns out
to be invented, and a reader can act on it.
