# Reporting

## Structure

Follow the shape of the generated `report/dataset-report.md`:

```text
Identity          dataset_id, file count, total bytes
Formats           what was detected, and on what basis
Metadata files    recognised by convention
Tables            per column: shape, missing, null-like tokens, claim id
Identifiers       detected by pattern, with file and line
Not determined    ← the section people skip; do not skip it
Warnings          everything the checks flagged
```

## Rules

**Cite a claim id for every number.** `get_evidence` gives you them. A table of
numbers with no claim ids is indistinguishable from a table of guesses.

**Quote the `dataset_id`.** Results are only comparable between runs at equal
`dataset_id`. Without it a reader cannot tell which bytes you looked at.

**Write the "Not determined" section before the conclusions.** Writing it last
tempts you to leave out the things you would rather have known. Typical
contents:

- cross-file relationships between tables and metadata records;
- the meaning of any column, including units and controlled terms;
- whether null-like tokens denote missing values;
- whether any detected identifier resolves;
- any FAIR indicator — FAIR assessment is a separate, unimplemented profile.

**Report warnings as findings, not as noise.** An unrecognised format, a ragged
row, a decode failure and a `.csv` whose bytes are a PNG are all substantive.

**Separate the three missingness states.** Empty cells, null-like tokens, and
values. Merging them loses the distinction that matters most for data quality.

## Phrasings that keep you honest

| Instead of | Write |
| --- | --- |
| "the dataset has 48 male and female mice" | "`animals.csv` has 48 rows; `sex` is recorded for 36 and empty for 12 [clm_…]" |
| "strain is C57BL/6J" | "`strain` holds `C57BL/6J` in 45 rows and the token `NA` in 3; what `NA` denotes is not stated [clm_…]" |
| "the dataset is FAIR-compliant" | "FAIR assessment is not implemented in this version. A DOI-shaped identifier appears in `README.md:33` [clm_…]; whether it resolves was not checked" |
| "no metadata is available" | "no filename matched a recognised metadata convention; `README.md` and `dataset_description.json` were recognised [clm_…]" |
| "columns are typed correctly" | "`weight_g` holds integer-shaped tokens; `birth_date` holds string-shaped tokens, as no date format is declared [clm_…]" |

## Before you submit

- [ ] Every number carries a claim id.
- [ ] The `dataset_id` appears.
- [ ] The "Not determined" section is present and honest.
- [ ] No value appears that is absent from the dataset's bytes.
- [ ] Warnings are reported.
- [ ] Nothing implies a FAIR verdict, an identifier resolution, or a vocabulary
      mapping — none of those are implemented.
