# XP14 benchmark package generators

Public code that rebuilds the **private** XP14 benchmark package from the
**private** XP14 source snapshot. These scripts contain no measurements and no
biological identifiers; they are the inspectable implementation behind
`benchmarks/xp14_apa/`, which is git-ignored (see issue #19).

Relates to D2A-17. The package they build is described in
`benchmarks/xp14_apa/README.md` — local-only.

## Why the generators are in the repo and the package is not

The package holds unpublished experimental data. The generators hold the logic.
Anyone with the private source can rebuild a byte-identical package, so the
benchmark stays reviewable and reproducible without the bytes being published.

Verified: re-running the full chain reproduces `manifest.json`, `checksums.json`
and all eight gold artifacts byte-for-byte, and leaves the source snapshot
untouched.

## Running

```bash
python scripts/benchmarks/xp14/01_freeze.py          # checksums + dataset_id + file classes
python scripts/benchmarks/xp14/02_build_gold.py      # animals, crosswalk, sessions
python scripts/benchmarks/xp14/03_build_statements.py# semantic statements (JSONL)
python scripts/benchmarks/xp14/04_build_manifest.py  # package manifest; consumes 01's temp file
python scripts/benchmarks/xp14/06_perturb.py         # derived perturbation cases (issue #20)
```

Order matters: `04` consumes a temporary file written by `01` and deletes it.
`06` is independent of `02`–`04`: it reads only `source/` and
`perturbations/perturbations.yaml`, and writes outside the package.

Scoring is separate, because it runs against a *result* rather than the package:

```bash
python scripts/benchmarks/xp14/07_score.py <assessment.json> [--json score.json]
```

It reads `gold/fair_expected.json` and reports agreement, movement since the
freeze, and — on its own line, failing the run by itself — any indicator the
gold forbids resolving. Exit status is 0 only when everything agrees, nothing
regressed, and nothing was resolved that must stay `unknown`.

Two subtleties worth knowing before reading a score:

- **Agreement is scored against `expected_result`, never `v0_1_actual`.** The
  gold says so itself. Where they differ, v0.1 has a known gap and a better
  assessor may legitimately beat it.
- **Whether v0.1 was right at freeze is the gold's `agrees` flag, not a
  comparison of verdict letters.** `I1-DATA-FORMATS-OPEN` returned `fail` at
  freeze and is marked `agrees: false`, because the verdict was right while the
  evidence under it was not — only the 9 files named `.xlsx` were evaluated and
  the 20 OOXML files named `.xls` escaped the check. Scoring closure off the
  letter would call that gap closed the day it was found and hide it the day it
  was fixed.

Running an agent condition, which produces an assessment in the same shape:

```bash
python scripts/benchmarks/xp14/08_run_agent.py \
    --ingest <ingest-dir> --out <run-dir> \
    --mode structured --model claude-sonnet-5
python scripts/benchmarks/xp14/07_score.py <run-dir>/assessment.json
```

The server is passed to the host per-run with `--mcp-config` and
`--strict-mcp-config`, never registered globally. Without the strict flag the
host also loads whatever the operator happens to have configured, and a `raw`
condition with three unrelated servers attached is not a raw condition. It also
leaves the operator's own configuration untouched, which a benchmark has no
business editing.

The prompt is identical in every mode and lives in `prompt_fair.md`, so it is
reviewable as the instrument it is. It names the twelve rule ids, because a
verdict that cannot be lined up against the gold cannot be scored. It
deliberately omits their operational definitions: supplying those would erase
the difference between `structured` and `fair-rules`, where reading the
canonical registry is exactly what the mode adds. It says nothing about which
indicators ought to be `unknown` — that is the measurement, not the setup.

Paths resolve from the script location. Set `D2A_REPO` to override if the
scripts are vendored elsewhere.

### Expected output

```text
dataset_id: sha256:d0bd1a8ea46e797a28517b2cb90319eb848a5dde7de4e58b09c54b28a50cf6d1
files: 36   total bytes: 9777575
animals.csv 15 · identifier_crosswalk.csv 15 · sessions.csv 75
semantic_statements.jsonl 158
file classes: analysis_output 21 · data 10 · metadata 2 · presentation_artifact 3
```

If `dataset_id` differs, the source snapshot has drifted and nothing downstream
should be trusted until that is explained.

## Design notes

**Nothing is repaired.** Incorrect values, contradictory metadata and broken
formulas are carried through verbatim. Facts the source cannot settle are
emitted as `unknown`, `absent_from_dataset` or `PENDING-A<n>` and never filled
in — the four open owner questions live in
`benchmarks/xp14_apa/adjudication/decisions.yaml`.

**The generators re-derive, they do not transcribe.** Every gold row is read
from the snapshot on each run and cross-checks are printed (15 animals, 75
sessions, 2 colliding local ids, 6 animals with no biological id). This is not
ceremony: the cross-check in `02_build_gold.py` caught a real bug in that script,
which had joined batch-1 `2-I` to a **batch-2** biological identifier because it
keyed on `local_id` alone. That is anomaly **XP14-A007** — local identifiers are
unique only within a batch — reproducing itself inside the tooling built to
describe it.

The guard that fixes it is deliberately commented in place rather than quietly
applied:

```python
# APA_Batch2_BW_IDs.xlsx covers batch 2 ONLY. Keying on local_id alone would
# match batch-1 '2-I'/'2-II' to batch-2 animals, because those literals
# collide across batches -- the defect this dataset is meant to expose.
bw_hit = bw_map.get(local) if batch == "2" else None
```

Keep that history. It is evidence that A007 is a real difficulty rather than a
contrived one.

**OOXML is opened by bytes, not by extension.** Twenty source files are named
`.xls` and contain OOXML, so the generators read each file's bytes into
`io.BytesIO` and hand that to `openpyxl`, bypassing its extension check.
`openpyxl.load_workbook(path)` refuses these files outright. This is anomaly
**XP14-A001** and the reason for D2A-46.

**Perturbations are materialised, never applied in place.** `06_perturb.py`
turns each of the 16 specifications in `perturbations/perturbations.yaml` into
its own derived dataset under `benchmarks/xp14_perturbed/<id>/source/`, with a
`provenance.json` beside it carrying the parent id, the derived id, the
generator version and the gold expectation *copied out of the spec*. The frozen
`source/` tree is digested before and after every run and the script aborts if a
single byte moved. `--verify` materialises the whole suite a second time into a
temporary directory and compares every digest, which is how the reproducibility
claim is checked rather than asserted.

Workbook cells are edited by surgery on the OOXML package XML, reassembled by a
ZIP writer that re-emits every untouched member's already-compressed bytes. A
load-and-save round trip through `openpyxl` was rejected: it would drop the six
charts in `230807_PT_MM.xls`, the pivot cache in `APA-ALL-results_MM.xlsx` and
every `printerSettings` part, so a case meant to perturb one cell would perturb
the whole file. Each derived workbook therefore differs from its parent in two
or three package parts and nowhere else.

**`01_freeze.py` reuses the repository's own primitives** —
`data2agent.ingest.checksum.dataset_id` and `.formats.detect` — so the package's
`dataset_id` is the same value `data2agent ingest` produces, rather than a
second implementation that might drift.

## File classes

`01_freeze.py` assigns each file one of `data`, `metadata`, `protocol_evidence`,
`analysis_output`, `presentation_artifact`, by observed role rather than by
extension. XP14 forced this: rotation speed, chance level and an
analysis-affecting exclusion rule exist only inside `.pptx` files, so
presentation artifacts are a load-bearing metadata carrier here and cannot be
treated as optional prose.

The rules are a small ordered table at the top of `01_freeze.py`. They are
specific to XP14 and are **not** a general classifier — generalising this is
D2A-49b, and belongs in `src/`, not here.

## Scope

These scripts are benchmark tooling. They are deliberately **not** part of
`src/data2agent/`: they are dataset-specific, they depend on `openpyxl`, and the
ingest core is dependency-free by policy (D2A-70). Nothing in `src/` was changed
to produce this package.
