# Reporting

## Structure

Follow the shape of the generated `report/dataset-report.md`:

```text
Identity              dataset_id, file count, total bytes
This ingest run       when, how long, which tool version
Missing-value         which convention was applied, and where it came from
Formats               what was detected, and on what basis
Metadata files        recognised by convention
Tables                per column: shape, missing (empty/tokens), unresolved, claim id
Identifiers           detected by pattern, with file and line
Not determined        ← the section people skip; do not skip it
Warnings              everything the checks flagged
```

If you ran a FAIR assessment, add a section for it — results, rationales, and
the `unknown`s left as unknown.

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
- whether *unresolved* tokens (`unknown`, `-`, `?`) denote missing values;
- whether any detected identifier resolves;
- any FAIR indicator — FAIR assessment is a separate, unimplemented profile.

**Report warnings as findings, not as noise.** An unrecognised format, a ragged
row, a decode failure and a `.csv` whose bytes are a PNG are all substantive.

**Separate the missingness states, and name the convention.** Empty cells,
resolved sentinel tokens, unresolved ambiguous tokens, and values. "3 missing" is
not reproducible on its own; "3 missing (0 empty, 3 resolved from `NA` under
`default-sentinels`)" is.

**Never report a FAIR `unknown` as anything else.** Nor a `pass` as broader than
the rule's own question.

## Phrasings that keep you honest

| Instead of | Write |
| --- | --- |
| "the dataset has 48 male and female mice" | "`animals.csv` has 48 rows; `sex` is recorded for 36 and empty for 12 [clm_…]" |
| "strain is C57BL/6J, 3 missing" | "`strain` holds `C57BL/6J` in 45 rows; 3 hold `NA`, resolved to missing under the `default-sentinels` convention because the dataset declares none of its own [clm_…]" |
| "15 values are missing" | "12 missing (empty cells) in `sex`, 3 missing (resolved `NA`) in `strain` — two different columns and two different reasons" |
| "the dataset is FAIR-compliant" | "the FAIR profile returns 8 pass, 2 fail, 2 unknown. `I2-VOCABULARY-REFERENCED` and `R1.3-MISSING-VALUES-DECLARED` fail; `F1-PID-RESOLVABLE` and `A1-RETRIEVAL-PROTOCOL` are unknown because neither can be settled from a local snapshot" |
| "the DOI is probably fine" | "`F1-PID-RESOLVABLE` returned `unknown`: no network request was made. The identifier is syntactically valid [clm_…]" |
| "no metadata is available" | "no filename matched a recognised metadata convention; `README.md` and `dataset_description.json` were recognised [clm_…]" |
| "columns are typed correctly" | "`weight_g` holds integer-shaped tokens; `birth_date` holds string-shaped tokens, as no date format is declared [clm_…]" |

## Before you submit

- [ ] Every number carries a claim id.
- [ ] The `dataset_id` appears.
- [ ] Every missingness figure names the convention that produced it.
- [ ] The "Not determined" section is present and honest.
- [ ] No value appears that is absent from the dataset's bytes.
- [ ] Warnings are reported.
- [ ] Every FAIR `unknown` is still an unknown, with its reason.
- [ ] No FAIR `pass` has been widened beyond the rule's own question.
- [ ] Nothing implies an identifier resolution, a vocabulary mapping or a SHACL
      validation — none of those are implemented.
