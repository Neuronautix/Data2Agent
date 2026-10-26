# Annotation export contract

`data2agent export-annotations` re-expresses Data2Agent assessments (and, for a curated
dataset, the `apply` command's curation log) as **proposed annotations** for a FAIR-VCG
dataspace: a separate repository that collects original/curated dataset pairs and the
annotations that judge them. The verdicts are computed by code in the assessment; the
exporter only translates them. No language model is involved.

The dataspace's rules are its own (its `docs/domain-contract.md` and
`docs/annotation-guidelines.md`). The exporter never copies its schema or its records
into this repository: it *reads* the dataspace's `schemas/annotation.schema.json` at run
time when it is there, and the tests use a stand-in.

## Invocation

```text
data2agent export-annotations <ingest-output-dir>
    --dataspace <root> --dataset-path <dir relative to the dataspace>
    --family <dataset_family_id> --variant original|curated
    --agent-model <id>                      required, never defaulted
    [--created-by <str>]                    default "data2agent <version>"
    [--created-at <ISO-8601 UTC, Z>]        default now; pass it for reproducible output
    [--curation-log <path>]                 curated variant only
    [--overwrite]
```

`<ingest-output-dir>` must already hold an `assess` result. Every `assessment.json`
(profile `fair`) and `assessment.<profile>.json` in it is exported; each file states its
own profile id. Rule questions are read from the installed profile of that id, so a
profile that does not ship with this checkout cannot be exported (its criterion
descriptions are never guessed).

stdout is a JSON summary; warnings also go to stderr:

```json
{"dataset_id": "sha256:...", "dataset_family_id": "...", "variant": "original",
 "dataset_path": "data/original/...", "output_dir": "annotations/proposed",
 "written": 12, "by_type": {"fair_assessment": 12}, "files": ["<family>-original-F1-pid-metadata.json", ...],
 "removed": [], "profiles": [{"id": "fair", "version": "0.1.0", "results": 12, "exported": 12}],
 "skipped": [], "warnings": [], "schema_validated": true}
```

`schema_validated` is `false` (with a `schema_validation_note` and a visible warning) when
the dataspace has no `schemas/annotation.schema.json` or `jsonschema` (a dev-only
dependency, imported lazily) is not installed. The built-in invariant checks below still
run in that case.

Library entry points: `data2agent.export.export_annotations`, `build_annotations` (no
files written), `label_for_result`, `result_for_label`. Nothing in `data2agent` imports
`data2agent.export` except the CLI, lazily inside its handler
(`tests/export/test_annotation_safety.py` asserts this).

## Mapping

Result to `judgement.label`:

| Data2Agent result | Label |
| --- | --- |
| `pass` | `conformant` |
| `fail` | `non_conformant` |
| `partial` | `partial` |
| `unknown` | `insufficient_evidence` |
| `not_applicable` | `not_applicable` |

(The shipped profiles cannot emit `partial`; it is mapped so an assessment that uses it is
neither rejected nor guessed at. Any other result word is an error.)

Profile and rule to `annotation_type`:

| Source | annotation_type | Which results |
| --- | --- | --- |
| profile `fair`, or any profile other than `quality`, rule `principle` is a FAIR principle (`F1`, `A1.1`, `R1.3`, ...) | `fair_assessment`, `fair_dimension` from the letter: F findable, A accessible, I interoperable, R reusable | all of them |
| profile `quality` | `quality_issue` | only `fail`, `partial`, `unknown` |
| any other principle | none: rule skipped, with a warning and a `skipped` entry | n/a |
| curation log | `curation_validation` | one per verification check, plus `original_unchanged` and the cell round trip |

Why `quality_issue` skips passes: a `quality_issue` record says "here is an issue". A
passing quality rule found no issue, and exporting it as a `conformant` issue would make
the dataspace's issue list say the opposite of what it means. `not_applicable` is skipped
for the same reason. FAIR records are different: a FAIR assessment is a judgement per
criterion, so a pass is itself the finding.

A principle counts as FAIR only on a whole-string match (`F`, `A`, `I`, `R`, optionally
followed by digits and dotted digits). A profile's own category label that merely *starts*
with one of those letters ("Reproducibility") is skipped rather than guessed into a
dimension. Each skipped rule is listed in the summary.

### Record fields

| Field | Value |
| --- | --- |
| `schema_version` | `0.1.0` |
| `annotation_id` | `<family>-<variant>-<code or family>-<slug in lower case>` (see [Shared ids](#shared-ids)); `-<n>` (1-based) appended when a quality result yields several records. Beyond that convention ids are never rewritten: one that cannot be schema-legal (`A-Za-z0-9._-`, first character a letter or digit) refuses the export. A collision (including two ids that differ only in case) is an error. |
| `dataset` | `{dataset_family_id, variant, version = the Data2Agent dataset_id}` |
| `target` | `dataset` at `--dataset-path` for FAIR and curation records. Quality: see offenders below |
| `criterion` | `{id = the shared criterion id (see [Shared ids](#shared-ids)), description = the rule's question from the profile}` |
| `judgement` | `{label, rationale}`; the result's rationale, whitespace-normalised. When there is none (typically a pass), a fixed sentence naming the rule, the result and the evidence check ids it cited. Never empty |
| `evidence` | see below |
| `provenance` | `method: agent_proposal`, `created_by`, `created_at`, `agent_model`, `notes` (a fixed sentence: profile, the Data2Agent rule id, result, `inferred`, dataset id, "computed by code, not by a model"; for curation records, the log field instead of the rule) |
| `review` | `{status: draft}` |

### Shared ids

`criterion.id` and `annotation_id` follow a convention agreed with the dataspace owner, so
that these records and those of other harnesses (an LLM harness writes, for example,
`phenotyper-original-R1.1-license`) can be compared in one corpus. One function,
`shared_ids` in `src/data2agent/export/annotations.py`, applies it:

| Source | `criterion.id` | `annotation_id` |
| --- | --- | --- |
| FAIR rule `<principle>-<SLUG>` | `FAIR-<principle>_<SLUG>` | `<family>-<variant>-<principle>-<slug>` |
| quality rule `Q-<SLUG>` | `QC_<SLUG>` | `<family>-<variant>-QC-<slug>` |
| curation log | `CV_<SLUG>` | `<family>-<variant>-CV-<slug>` |

- A FAIR rule id is split on its **first** hyphen only: principles carry dots (`R1.3`),
  never hyphens. A rule whose id does not start with its own principle keeps its whole id
  as the slug (`A-FINDABLE` graded under F1 becomes `FAIR-F1_A-FINDABLE`).
- Quality rules carry a category (`Q.headers`), not a code, so they take no code; only the
  `Q-` prefix is dropped. A quality rule id without it keeps its whole id.
- The exported slug spells `LICENCE` as `LICENSE` (GitHub, SPDX), whole words only, through
  the `EXPORT_SPELLING` map. The Data2Agent rule id keeps its own spelling, and the record
  names it in `provenance.notes`.

| Data2Agent rule | `criterion.id` | `annotation_id` (family `phenotyper`, variant `original`) |
| --- | --- | --- |
| `F1-PID-METADATA` | `FAIR-F1_PID-METADATA` | `phenotyper-original-F1-pid-metadata` |
| `F1-PID-RESOLVABLE` | `FAIR-F1_PID-RESOLVABLE` | `phenotyper-original-F1-pid-resolvable` |
| `R1.1-LICENCE-DECLARED` | `FAIR-R1.1_LICENSE-DECLARED` | `phenotyper-original-R1.1-license-declared` |
| `R1.3-MISSING-VALUES-DECLARED` | `FAIR-R1.3_MISSING-VALUES-DECLARED` | `phenotyper-original-R1.3-missing-values-declared` |
| `Q-HEADERS-PORTABLE` (offenders 1, 2) | `QC_HEADERS-PORTABLE` | `phenotyper-original-QC-headers-portable-1`, `-2` |

Key order is fixed, output is UTF-8 with `\n` newlines, and files are written in
annotation-id order, so the same inputs plus `--created-at` give byte-identical files.

### Quality offenders

If a quality result carries `observations.offenders` (a list), one annotation is written
per distinct offender. Recognised offender shapes:

- `{"file": <manifest path>, "field": <name>}` (aliases `path`/`table`/`source` for the
  file; `field_name`/`column`/`header` for the field): target `field` with `field_name`;
- `{"file": <manifest path>}` or a bare string that is a manifest path: target `file`;
- `{"field": <name>}` or any other bare string: target `field` with `field_name`, scoped to
  the single file the result cites when there is exactly one, else to the dataset
  directory.

Anything else (a file not in the manifest, a number, ...) is skipped with a warning. If no
offender can be read, or there are none, one annotation targets the dataset.

### Evidence

Every record has at least one entry `{locator, checksum, description}`:

- The files a result cites: claim ids are resolved through the ingest `evidence.json`
  (`source`, pinned against `source_sha256`), and check items are scanned for file paths that
  exactly match the manifest. A quality offender's own file leads its entry list.
- `locator` is `<--dataset-path>/<manifest path>`, `checksum` is the manifest's sha256 for
  that file, and `description` says what examined it (the cited claim texts or the check's
  detail). The cited file must exist under the dataspace; a size that differs from the
  manifest is a warning (line-ending conversion, say), since the pinned checksum is the
  manifest's.
- A file whose name cannot be a legal locator (characters outside `A-Za-z0-9._/-#[]=`, or
  `..`) is not cited by path. An entry pointing at the dataset directory says so and
  quotes the name and sha256 in its description. The file must still exist under the
  dataspace, exactly as for a citable one.
- A result that cites no file (for example "no file was recognised as metadata") gets one
  entry at the dataset directory, without a checksum, whose description says that the rule
  examined dataset-level facts of that `dataset_id`. A directory has no byte checksum, and
  inventing one from an arbitrary file would misstate what was examined.

### Curation log

`--curation-log` takes the log written by `apply` (`log_version "1"`) and requires
`--variant curated`. The exporter refuses a log whose `result.dataset_id` differs from the
ingested dataset, or whose `result.files` do not match the manifest exactly: every entry
must carry the manifest's checksum (the curated bytes changed after the log otherwise), no
path may be listed twice, and no manifest file may be missing, so the log describes the
complete curated dataset. It writes:

| Criterion id | Label |
| --- | --- |
| `CV_ORIGINAL-UNCHANGED` | `conformant` if `original_unchanged` is true, else `non_conformant` |
| `CV_CELL-ROUND-TRIP` | `conformant` if `cells_round_trip == cells_compared`, `non_conformant` if fewer, `insufficient_evidence` when nothing was compared |
| `CV_<check id>` | `conformant` for `pass`, `non_conformant` for `fail`; rationale is the check's `detail` |

Evidence is the curated result files. `changes` and `open_items` are not exported.

## Safety rules

These are enforced in code, and each has a test.

- Agent output is a proposal: `provenance.method` is `agent_proposal`, `review.status` is
  `draft`. A produced record with any other status or method, a `reviewed_*` field, no
  agent model, an empty evidence list or rationale, or an illegal locator is a hard
  internal error (`ExportInternalError`); nothing is corrected or downgraded.
- `--dataset-path` is refused if absolute (POSIX, Windows drive or UNC), if it contains
  `..`, a backslash, an empty or `.` segment, a character outside the locator pattern, if it
  does not exist as a directory under `--dataspace`, or if it resolves outside it.
- The output directory must resolve to exactly `<dataspace>/annotations/proposed`, and must
  already exist (it is never created). Nothing is ever written to `annotations/curated/` or
  under `data/`.
- An existing annotation file is never overwritten unless `--overwrite`, and even with it
  never one that is not an untouched draft agent proposal (a reviewer moved it to
  `in_review`/`rejected`, a human authored it, or it is unreadable).
- With `--overwrite`, earlier drafts of the same export that the run no longer produces
  (a failure that now passes, fewer offenders, a dropped curation check) are removed in the
  same all-or-nothing step and listed under `removed` in the summary. Only untouched
  Data2Agent draft proposals for the same family, variant, dataset version and annotation
  type qualify; a reviewed or human record, another harness's, another version's, or a type
  this run does not write is never removed.
- All-or-nothing: paths are checked, every record is built and invariant-checked, and all
  of them are validated against the dataspace schema **before the first file is written**.
  Files are then staged under temporary names and renamed, and staging is cleaned up on
  failure.
- `--agent-model` is required (argparse rejects its absence; a blank value is refused). It
  is never defaulted or guessed.
- Validation: if `<dataspace>/schemas/annotation.schema.json` exists and `jsonschema` is
  importable, every record is validated (Draft 2020-12) and any error refuses the export.

## What the exporter refuses

An absolute, traversing, missing or non-locator `--dataset-path`; a dataspace without an
`annotations/proposed/` directory; an output directory that is not exactly that one; an
existing file (without `--overwrite`), or a reviewed one (with it); a missing or blank
agent model; a `--created-at` that is not ISO-8601 UTC with a `Z`; an assessment for another
dataset than the ingest output; an assessment file whose name contradicts its profile id; a
profile that cannot be loaded, a rule the profile does not define, or a result word outside
the table; a family, rule or check id that cannot form a legal `annotation_id`; an annotation id
collision; a cited file missing from the dataspace; a curation log that is malformed,
mismatched or paired with `--variant original`; any schema error; and any invariant break.

## Reference labels (for scoring tools only)

`data2agent.export.reference_labels.read_reference_labels(dataspace_root, family, variant)`
returns `{criterion id: label}` from records under `annotations/curated/` with
`review.status == "approved"` for that family and variant. It ignores `deprecated` records,
other families and variants, and everything under `annotations/proposed/`. It raises when two
approved records give one criterion different labels (so a criterion judged differently on
several targets needs a target-aware reader), when an approved record is an
`agent_proposal`, or when a curated file cannot be parsed.

**Holdout answer protection.** This reader exists so a *scorer* can compare an agent's
output with the human-approved answers after the agent has finished. It must never be
exposed to an agent under evaluation: not through an MCP tool, a prompt, a log or a
fixture. The dataspace's `docs/annotation-guidelines.md` states that leaking curated labels,
scores or rationale for a holdout family invalidates the benchmark for that family. It is
therefore a separate module, not re-exported from `data2agent.export`, and not imported by
anything else in the package.

`result_for_label(label, partial_as=None)` is the reverse mapping for scorers:
`conformant`/`non_conformant`/`not_applicable` map back to `pass`/`fail`/`not_applicable`;
`insufficient_evidence` maps to `unknown`, never to `fail`; `partial` has no Data2Agent
equivalent and stays `"partial"` unless the scorer says how it wants it collapsed
(`partial_as="pass"|"fail"|"unknown"`).
