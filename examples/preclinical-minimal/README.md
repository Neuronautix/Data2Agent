# Preclinical minimal example dataset

A small, synthetic behavioural dataset used as the Data2Agent v0.1 vertical
slice. It is **not real data**: it was generated so that the acceptance tests can
assert exact numbers, and so that the "unknown stays unknown" rule has something
to bite on.

## Contents

| File | Description |
| --- | --- |
| `animals.csv` | One row per animal (48 rows). |
| `observations.csv` | Three behavioural sessions per animal (144 rows). |
| `dataset_description.json` | Dataset-level metadata, BIDS-style filename. |

## Deliberate imperfections

These are the point of the example, not defects to be cleaned up:

- `sex` is **empty** for 12 of the 48 animals. The recording sheet for the last
  cohort was lost. Data2Agent must report 12 missing values and must not infer
  any of them.
- `strain` holds the literal token `NA` for three animals, and this dataset
  **never declares what `NA` means**. Data2Agent resolves it to missing under
  its built-in `default-sentinels` convention, records that it did, and keeps
  `missing_empty` and `missing_sentinel` apart — so the 3 is reproducible by
  anyone who knows the rule. The FAIR profile reports the undeclared convention
  as `R1.3-MISSING-VALUES-DECLARED: fail`, which is the correct finding: a
  reader cannot tell "not recorded" from "not applicable" from a strain named
  NA, and every other tool will guess differently.
- `latency_s` is empty where a trial was aborted. No abort reason is recorded.
- No units are declared for `weight_g` beyond the column name, and no controlled
  vocabulary is referenced for `genotype`. Both are unknown, not inferable.

## Identifiers appearing in this dataset

- Dataset DOI (fictional): 10.5281/zenodo.0000000
- Contact ORCID (fictional): 0000-0002-1825-0097
- Mouse strain RRID (fictional): RRID:IMSR_JAX:000664

## Expected assessment

```text
8 pass, 2 fail, 2 unknown

fail     I2-VOCABULARY-REFERENCED       no vocabulary namespace in the metadata
fail     R1.3-MISSING-VALUES-DECLARED   NA used but never declared
unknown  F1-PID-RESOLVABLE              needs a network request
unknown  A1-RETRIEVAL-PROTOCOL          needs the dataset's published location
```

The two unknowns are the point as much as the failures: neither can be settled
from a local snapshot, so neither is guessed at.

## Usage

```bash
data2agent ingest examples/preclinical-minimal -o /tmp/preclinical-agent
data2agent assess /tmp/preclinical-agent
data2agent serve  /tmp/preclinical-agent --mode fair-deterministic
```

To see the missing-value convention change the numbers:

```bash
data2agent ingest examples/preclinical-minimal -o /tmp/strict --strict-missing
# strain: missing 0  (NA is left as a value)
```
