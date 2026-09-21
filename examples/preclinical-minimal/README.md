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
- `strain` holds the literal token `NA` for three animals. Whether `NA` here
  means "not recorded", "not applicable" or a strain named NA is **not stated
  anywhere in this dataset**, so Data2Agent counts those tokens separately from
  empty cells and refuses to fold them into the missing count.
- `latency_s` is empty where a trial was aborted. No abort reason is recorded.
- No units are declared for `weight_g` beyond the column name, and no controlled
  vocabulary is referenced for `genotype`. Both are unknown, not inferable.

## Identifiers appearing in this dataset

- Dataset DOI (fictional): 10.5281/zenodo.0000000
- Contact ORCID (fictional): 0000-0002-1825-0097
- Mouse strain RRID (fictional): RRID:IMSR_JAX:000664

## Usage

```bash
data2agent ingest examples/preclinical-minimal -o /tmp/preclinical-agent
data2agent serve /tmp/preclinical-agent --mode structured
```
