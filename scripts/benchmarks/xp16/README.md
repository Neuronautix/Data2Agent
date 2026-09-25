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
├── 06_ingest.py        ingest under a declared condition; records declaration sha256s
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
├── config/conditions.json          ingest conditions: undeclared / declared
├── config/layouts.json             declared table headers (`ingest --layout`)
├── config/relationships.json       declared relationships (`relationships --declarations`)
├── config/crosswalk.csv            declared identifier crosswalk (`--crosswalk NAME=...`)
├── ingest_<condition>/             ingest output + condition.json (declaration sha256s)
└── results/, runs/                 baseline reports, agent runs, scores
```

## Running

```bash
python scripts/benchmarks/xp16/01_freeze.py  --source <origin-dir> --package <pkg>
python scripts/benchmarks/xp16/02_build_gold.py --package <pkg>
python scripts/benchmarks/xp16/02_build_gold.py --package <pkg> --check   # byte-identical?

python scripts/benchmarks/xp16/06_ingest.py --package <pkg> --condition declared
python scripts/benchmarks/xp16/06_ingest.py --package <pkg> --condition undeclared
python scripts/benchmarks/xp16/05_baseline.py --package <pkg> --ingest <pkg>/ingest_declared \
    --out <pkg>/results/baseline_declared.json

python scripts/benchmarks/xp16/04_run_agent.py --package <pkg> --ingest <pkg>/ingest_declared \
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
scorer never uses it. "unknown" is a valid owner answer.

What an answer does is declared per question, never inferred from the decision
merely being answered:

```yaml
blocked_by: [OQ1]
on_answer:
  OQ1:
    yes: compute                  # hypothesis held: the provisional computation becomes gold
    no: {op: count, table: t2}    # or: abstain
    unknown: abstain              # owner-confirmed unknown -> "cannot be determined"
```

The answer key is the decision's `answer_key` (a short handle for a prose
answer) or its normalised `answer`. An answered decision whose answer the
question does not map fails the build.

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
  citing one cell must be what that cell holds. A source is `verified` only with
  a matching sha256 and an existing locator; a citation set takes the status of
  its weakest source (`invalid` < `sha_unverified` < `sha_only` < `verified`),
  and per-source statuses are kept in the report.

Blank identifiers and blank group keys (None or whitespace only) are never
counted as units, distinct values or groups, in every counting path.

Breakdowns are reported per category and per capability tag.

## Ingest conditions

The ingest is part of the benchmark condition. `06_ingest.py` runs
`data2agent ingest` and `data2agent relationships` exactly as
`config/conditions.json` declares, and writes `condition.json` beside the
output: the sha256 of the layout, relationship and crosswalk declarations, the
dataset_id, the data2agent version and commit, and the commands. The baseline
report and every agent run copy it, so a number is always tied to one exact
configuration; a manifest whose recorded layout sha256 is not the declared
file's is refused.

- **undeclared** -- no declarations; measures whether the tool finds headers
  and identities by itself.
- **declared** -- the declared layout, relationships and crosswalk; the agent
  is given the right tables, so discovering layout is not what is measured:
  retrieval, computation, joins and abstention are.

Report both when comparing builds; use **declared** as the headline condition
for agent runs, because it isolates the data-plane skills this benchmark is
about from layout discovery, which the undeclared condition tracks separately.

A condition may also re-address declared gold tables through `service_tables`
(for instance a sheet declared as blocks, `<file>#<sheet>#<block>`); an unknown
gold table id is refused. A top-level `service_tables` in `conditions.json`
applies to every condition (a key that does not depend on declarations, such as
a BORIS project's `<file>#intervals` table), and a condition's own entries
override it. BORIS tables are matched to the gold by column name (they have no
sheet positions) and their rows are located the service's way: observation id +
start/stop event index. `--data2agent-src <tree>/src` runs (06) and evaluates
(05) another data2agent tree, such as an unmerged branch under review; the
condition record and the report then name that tree and claim no commit, so
such a number can never pass for one on the repository's own build.

## Baseline

`05_baseline.py` re-runs every gold computation with rows obtained from
`DatasetService.read_rows` instead of the independent reader. A gold column is
matched to the service column at the same physical position whose header parts
end with the gold's (extra upper labels allowed, fewer not; dedupe suffixes
ignored), so naming conventions do not fail a question but a wrong header row
or a missing upper label does; `--names-as-is` restores name-only matching,
which can pass a question by reading a same-named column elsewhere in the
sheet. A failure is classified by the retrieval gap behind it (`header_detection`, `multi_table_sheet`, `boris_tsv_preamble`,
...). A cell above a table's data (a block title, a banner) is read the way an
agent would read it, through `inspect_table(..., include_rows_above_data=True)`.

For aggregation questions it also makes one native service call and scores it
against the gold's n (units), mean and sem: `aggregate` with the declared `unit`
and per-unit reduction, or `aggregate_join` when the grouping column lives in
another table -- through the declared crosswalk when the gold join uses an
identifier transform (`transform_crosswalks` in the ingest condition). A table
whose service rows extend beyond the declared data rows (another block, a
footer) is reported as `mixes_blocks` rather than aggregated. Pending questions
are probed against their provisional answer. For abstention and pending
questions it also checks whether the evidence an agent would need is
retrievable. The report lists every service tool the run used.

## Scope

Benchmark tooling only. Nothing in `src/` is changed or depended on beyond the
checksum fold, the service the baseline drives, and the MCP modes the runner
reuses through XP14's harness.
