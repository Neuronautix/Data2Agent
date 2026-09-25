# XP16 data-plane benchmark generators

Public code that builds and scores the **private** XP16 benchmark: does an agent
using the Data2Agent MCP tools (`read_rows`, `filter_rows`, `aggregate`,
`join_tables`, relationships) retrieve and compute the *right numbers* from a
real preclinical dataset, cite where they came from, and abstain where the data
cannot decide?

XP14 benchmarks the FAIR-assessment plane (verdicts per indicator). XP16
benchmarks the data plane (values, sets, group statistics, cross-file checks).

These scripts contain **no data**: no file, sheet or column names from the
dataset, no identifiers, no values, no expected answers. Everything
dataset-specific lives in the package, which is git-ignored under
`benchmarks/` like XP14's (issue #19).

## Layout

```text
scripts/benchmarks/xp16/            (tracked, data-free)
├── xp16lib.py          independent table reader + the closed gold-computation set
├── 01_freeze.py        byte-for-byte snapshot, checksums, dataset_id
├── 02_build_gold.py    expected answers + citations from the snapshot (--check: rebuild is byte-identical)
├── 03_score.py         scorer: correctness, abstention, citations, per-capability breakdown
├── 04_run_agent.py     runs one agent condition through the MCP server (reuses xp14/08_run_agent.py)
├── 05_baseline.py      deterministic baseline through DatasetService (no LLM)
└── prompt_qa.md        the prompt template; questions are substituted at run time

benchmarks/<package>/               (local only, git-ignored)
├── source/                         frozen snapshot
├── checksums.json                  sha256 per file + dataset_id
├── config/tables.yaml              declared tables: file, sheet, header row(s), data rows
├── config/questions.spec.yaml      question text + gold computation per question
├── config/audit_facts.spec.yaml    facts the audit and owner questions cite
├── adjudication/decisions.yaml     owner questions; the only place ambiguity is resolved
├── gold/questions.yaml             GENERATED expected answers, sources, computations
├── gold/audit_facts.yaml           GENERATED
├── ingest/                         `data2agent ingest` output, for the baseline and runs
└── results/, runs/                 baseline reports, agent runs, scores
```

## Running

```bash
python scripts/benchmarks/xp16/01_freeze.py  --source <origin-dir> --package <pkg>
python scripts/benchmarks/xp16/02_build_gold.py --package <pkg>
python scripts/benchmarks/xp16/02_build_gold.py --package <pkg> --check   # byte-identical?

data2agent ingest <pkg>/source -o <pkg>/ingest --mode structured
python scripts/benchmarks/xp16/05_baseline.py --package <pkg> --ingest <pkg>/ingest \
    --out <pkg>/results/baseline.json

python scripts/benchmarks/xp16/04_run_agent.py --package <pkg> --ingest <pkg>/ingest \
    --out <pkg>/runs/<cell> --mode structured --model <model> [--capabilities a,b]
python scripts/benchmarks/xp16/03_score.py --questions <pkg>/gold/questions.yaml \
    --answers <pkg>/runs/<cell>/answers.json --package <pkg> [--capabilities a,b]
```

`04_run_agent.py --dry-run` writes the exact prompt without calling a host.

## Independence of the gold

The gold must not be computed by the system under test, or the benchmark grades
Data2Agent against itself. `xp16lib.FileRowSource` reads workbooks with
`openpyxl` (by bytes, so an extension never decides how a file is opened) and
delimited exports with the standard `csv` module. `data2agent` is imported in
exactly one generator, `01_freeze.py`, and only for its checksum fold, so the
package's `dataset_id` equals what `data2agent ingest` reports.

The builder verifies every file against `checksums.json` before reading and
again afterwards, and writes nothing time-dependent, so `--check` can prove a
rebuild is byte-identical.

## Declaring tables

A table is declared, never guessed: which rows hold the header, which rows hold
data. Several header rows are joined with ` | ` (upper rows may be
forward-filled, preamble labels dropped), duplicate names get a ` #2` suffix,
and template columns can be derived:

```yaml
tables:
  weights:
    file: sub/registry.xlsx
    sheet: Sheet1
    header_rows: [3]          # the real header sits under a banner row
    first_row: 4
    last_row: 28
    derived: {Animal: '{Cage}-{Tail}'}
    service_table: 'sub/registry.xlsx#Sheet1'   # used by the baseline only
  export:
    kind: delimited
    file: sub/export.tsv
    delimiter: "\t"
    header_rows: [2, 3]       # behaviour row + measure row -> 'event | Total duration'
    drop: ['Behaviors:', 'Subjects:']
    first_row: 4
transforms:                   # declared identifier rewrites; unmatched -> None
  registry_to_measure: {regex: '(?P<s>[A-Z])-(?P<c>\d+)_(?P<t>\w+)', template: '{s}{c}-{t}'}
```

## Gold computations

A closed set, evaluated by `xp16lib.evaluate`. Each returns the answer, the
sources it read (file sha256, sheet, cell range, rows) and a plain-language
computation string, all written into the gold.

| op | answer |
|---|---|
| `cell` | the value of one column in the single row matching a filter |
| `raw_cell`, `raw_range` | a cell / distinct values of a column range, for sheet layout outside any table |
| `count`, `sum`, `values` | row count or distinct count; a column sum; distinct values (a set) |
| `group_count` | counts per group |
| `group_stats` | per-**unit** reduction (sum / mean), then mean, sd (n-1), sem per group; optional join |
| `compare` | one attribute unit by unit across two tables (optional per-unit reduce, value map) |
| `consistency` | one attribute across N tables; conflicting units |
| `sum_check` | does a recorded score equal the sum of the cells it claims to summarise |
| `set_difference`, `bundle`, `mapped` | combinators |
| `abstain` | expected answer is ABSTAIN; the evidence for it is *checked*, not asserted |

`group_stats` is where the unit of analysis is enforced: a unit whose rows fall
in two groups is an error, and `n` counts units, not rows.

## Owner questions and PENDING answers

An ambiguity is never filled from plausibility. It becomes an entry in
`adjudication/decisions.yaml` with its evidence and `status: open`. A question
that depends on it lists it in `blocked_by:` and its gold answer is
`PENDING-<ids>`. Its computation still runs and is stored apart as
`provisional_answer`, so the owner sees what the answer would become; the
scorer never uses it. Answering the owner question and rebuilding turns the
provisional answer into gold. "unknown" is a valid owner answer.

## Capability tags

Every question lists what it needs in `requires:`, from a closed vocabulary
(the builder rejects anything else):

| tag | the question needs |
|---|---|
| `read_rows`, `filter`, `aggregate`, `join` | the basic tools |
| `header_detection` | the real header below banner / title rows |
| `multi_table_sheet` | a data block whose own header row sits mid-sheet |
| `duplicate_headers` | two columns with the same header text told apart |
| `boris_tsv_preamble` | BORIS aggregated exports: preamble lines, two-row header |
| `boris_project` | `.boris` project files (JSON) |
| `crosswalk` | a declared mapping between identifier forms across files |
| `unit_aggregation` | reduce to one value per animal before summarising |
| `code_mapping` | a coded value mapped to its meaning as stated in the data |
| `cross_file_consistency` | the same fact compared across files |
| `formula_audit` | a recorded value recomputed from the cells it claims to use |
| `version_diff` | two versions / exports of the same data compared |
| `abstention` | recognising that the data cannot answer |

This is what lets the benchmark grow with the tool: `--capabilities` on the
scorer and runner restricts a run to the questions a build can be expected to
answer, and the baseline's `--missing-capabilities` predicts which questions
should fail today so the report shows where prediction and outcome disagree.

## Scoring

`03_score.py` keeps three things apart:

- **correctness** -- `number` (absolute / relative tolerance), `integer`,
  `boolean`, `exact`, `set` (order-, case- and whitespace-insensitive),
  `table` (rows matched by key columns, extra rows fail), `object` (declared
  fields only);
- **abstention** -- on ABSTAIN / PENDING gold, abstaining scores +1 and a
  confident answer **-1** (listed on its own line); abstaining on an answerable
  question scores 0 (over-abstention);
- **citation** -- the file must belong to the snapshot, a given sha256 must
  match, the sheet and cell / range / line must exist, and a scalar answer
  citing one cell must be what that cell holds. `verified` requires a matching
  sha256.

Breakdowns are reported per category and per capability tag.

## Baseline

`05_baseline.py` re-runs every gold computation with rows obtained from
`DatasetService.read_rows` instead of the independent reader. The service's
column names are used as they come; a failure is classified by the retrieval
gap behind it (`header_detection`, `multi_table_sheet`, `boris_tsv_preamble`,
...). For aggregation questions it also calls the service's own `aggregate`
tool once, which has no per-unit reduction and no SEM, and reports whether its
row count equals the gold's number of animals. For abstention and pending
questions it checks whether the evidence an agent would need is retrievable.

## Scope

Benchmark tooling only. Nothing in `src/` is changed or depended on beyond the
checksum fold, the service the baseline drives, and the MCP modes the runner
reuses through XP14's harness.
