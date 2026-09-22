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
```

Order matters: `04` consumes a temporary file written by `01` and deletes it.

Paths resolve from the script location. Set `D2A_REPO` to override if the
scripts are vendored elsewhere.

### Expected output

```text
dataset_id: sha256:d0bd1a8ea46e797a28517b2cb90319eb848a5dde7de4e58b09c54b28a50cf6d1
files: 36   total bytes: 9777575
animals.csv 15 · identifier_crosswalk.csv 15 · sessions.csv 75
semantic_statements.jsonl 162
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
