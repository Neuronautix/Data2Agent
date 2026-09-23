"""The ingestion pipeline.

    dataset -> inventory -> checksums -> immutable dataset identity

Ingestion is read-only, deterministic, and free of any language model. It emits
three documents:

``manifest.json``
    What the dataset *is*. Contains no timestamps, no absolute paths and no
    host details, so repeated ingests of identical bytes are byte-identical.
``provenance.json``
    What this particular *run* was: when, where, with which version.
``evidence.json``
    Every claim the manifest supports, bound to the check and bytes behind it.

Splitting the run-specific parts out of the manifest is a deliberate deviation
from the obvious design (an ``ingested_at`` field alongside ``dataset_id``): a
manifest that carries a clock reading cannot be compared for equality, and
"repeated ingest gives the same manifest" is the property the benchmark depends
on most.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from .. import MANIFEST_VERSION
from ..errors import OutputError
from ..evidence import EvidenceItem, EvidenceLedger
from . import conventions, formats, identifiers, metadata, structured, tabular
from .checksum import dataset_id as fold_dataset_id
from .checksum import hash_file
from .conventions import DEFAULT_CONVENTION, MissingValueConvention
from .inventory import DEFAULT_EXCLUDES, FileEntry, Inventory, build, walk
from .provenance import ProvenanceRecord, runtime_fingerprint, tool_fingerprint, utc_now

MANIFEST_FILENAME = "manifest.json"
PROVENANCE_FILENAME = "provenance.json"
EVIDENCE_FILENAME = "evidence.json"

# Files whose text we scan for identifiers. Scanning a 4 GB binary for DOIs is
# pointless; scanning a README is where identifiers actually live.
_IDENTIFIER_SCAN_FORMATS = (
    formats.TEXT_FORMATS | formats.STRUCTURED_FORMATS | formats.TABULAR_FORMATS
)


@dataclass
class IngestResult:
    dataset_id: str
    manifest: dict[str, Any]
    provenance: dict[str, Any]
    evidence: EvidenceLedger
    output_dir: Path

    @property
    def warnings(self) -> list[str]:
        return list(self.manifest.get("warnings", []))


def ingest(
    source: Path,
    output: Path,
    *,
    excludes: frozenset[str] = DEFAULT_EXCLUDES,
    convention: MissingValueConvention | None = None,
    write: bool = True,
) -> IngestResult:
    """Ingest ``source`` into ``output``, leaving ``source`` untouched.

    ``convention`` overrides how missing-value tokens are resolved. Left unset,
    the dataset's own declaration is used when it makes one, and the built-in
    default otherwise -- in every case the choice is recorded in the manifest
    and cited by each missingness claim.
    """
    started_at = utc_now()
    started_monotonic = perf_counter()
    source = Path(source).expanduser().resolve()
    output = Path(output).expanduser().resolve()

    if output == source or source in output.parents:
        raise OutputError(
            "output directory must sit outside the dataset source, so that ingestion "
            f"cannot alter what it is describing: {output}"
        )

    inventory = build(source, excludes=excludes)
    identity = fold_dataset_id((entry.path, entry.sha256) for entry in inventory.files)
    active_convention = convention or _discover_convention(source, inventory)

    ledger = EvidenceLedger(identity)
    warnings = list(inventory.warnings)

    tables: dict[str, Any] = {}
    structured_docs: dict[str, Any] = {}
    metadata_files: list[dict[str, Any]] = []
    metadata_candidates: list[dict[str, Any]] = []
    identifier_hits: list[dict[str, Any]] = []

    _record_dataset_claims(ledger, identity, inventory, active_convention)

    for entry in inventory.files:
        absolute = source / entry.path
        _record_file_claims(ledger, entry)

        # The filename verdict is taken first for every file, and travels with
        # the entry whichever rule finally recognised it: "recognised by content"
        # must never obscure "and the name matched nothing".
        classification = metadata.classify(entry.path)
        if classification is not None:
            metadata_files.append(classification.as_dict())
            ledger.record(
                f"'{entry.path}' matches the {classification.convention} metadata convention",
                subject=entry.path,
                evidence=[
                    EvidenceItem(
                        source=entry.path,
                        source_sha256=entry.sha256,
                        check="metadata.file-convention",
                        result=classification.convention,
                    )
                ],
            )

        if entry.format.format_id in formats.TABULAR_FORMATS:
            profile = tabular.profile_table(absolute, entry.path, active_convention)
            if profile is None:
                warnings.append(f"{entry.path}: could not be read as a delimited table")
            else:
                tables[entry.path] = profile.as_dict()
                warnings.extend(f"{entry.path}: {note}" for note in profile.warnings)
                _record_table_claims(ledger, entry, profile, active_convention)
                if classification is None:
                    _recognise_table(
                        ledger,
                        entry,
                        metadata_files,
                        path=entry.path,
                        rows=profile.rows,
                        columns=profile.columns,
                    )

        elif entry.format.format_id in formats.WORKBOOK_FORMATS:
            # Reached only after the format was established from the file's
            # bytes, so a workbook named '.xls' arrives here too (D2A-46).
            from ..readers import workbook as workbook_reader

            if not workbook_reader.available():
                # The core stays honest about what it cannot see. A workbook the
                # tool could not open is reported as exactly that -- never folded
                # into silence, which is what made this gap invisible before.
                warnings.append(
                    f"{entry.path}: identified as a workbook but not profiled; "
                    f"install the 'xlsx' extra to enable it"
                )
                # A workbook nobody opened is a question, not an answer. It is
                # registered as a metadata candidate so that "no metadata was
                # found" cannot be read off a file that was never examined.
                if classification is None:
                    _record_candidate(
                        ledger,
                        entry,
                        metadata_candidates,
                        path=entry.path,
                        reason="reader-unavailable",
                        note=(
                            "identified as a workbook and not opened, because the optional "
                            "'xlsx' reader is not installed; whether it carries metadata is "
                            "undetermined"
                        ),
                    )
                ledger.record(
                    f"'{entry.path}' is a workbook whose contents were not profiled",
                    subject=entry.path,
                    evidence=[
                        EvidenceItem(
                            source=entry.path,
                            source_sha256=entry.sha256,
                            check="workbook.reader-unavailable",
                            result={"format": entry.format.format_id, "extra": "xlsx"},
                        )
                    ],
                )
            else:
                for sheet in workbook_reader.profile_workbook(
                    absolute, entry.path, active_convention
                ):
                    tables[sheet.path] = sheet.as_dict()
                    warnings.extend(f"{sheet.path}: {note}" for note in sheet.warnings)
                    _record_sheet_claims(ledger, entry, sheet, active_convention)
                    if classification is not None:
                        continue
                    if not sheet.profiled:
                        _record_candidate(
                            ledger,
                            entry,
                            metadata_candidates,
                            path=sheet.path,
                            reason="unprofiled",
                            note=(
                                "content could not be read, so whether it carries metadata "
                                "is undetermined"
                            ),
                        )
                        continue
                    _recognise_table(
                        ledger,
                        entry,
                        metadata_files,
                        path=sheet.path,
                        rows=sheet.rows or 0,
                        columns=sheet.columns,
                        locator={"sheet": sheet.sheet, "header_row": sheet.header_row},
                    )

        elif entry.format.format_id in formats.STRUCTURED_FORMATS:
            document, parse_error = structured.load_json(absolute)
            profile = structured.profile_document(document, parse_error, relative_path=entry.path)
            structured_docs[entry.path] = profile.as_dict()
            if profile.parse_error:
                warnings.append(f"{entry.path}: JSON parse error: {profile.parse_error}")
                if classification is None:
                    # The metadata rule applied and could not run. Saying nothing
                    # would make an unreadable descriptor indistinguishable from
                    # a file that was read and found to carry none.
                    _record_candidate(
                        ledger,
                        entry,
                        metadata_candidates,
                        path=entry.path,
                        reason="unparsed",
                        note=(
                            f"JSON that could not be parsed, so whether it declares a "
                            f"metadata standard is undetermined: {profile.parse_error}"
                        ),
                    )
            elif classification is None:
                recognised = metadata.classify_json_document(entry.path, document)
                if recognised is not None:
                    _record_content_metadata(ledger, entry, metadata_files, recognised)
            ledger.record(
                f"'{entry.path}' is a JSON document with root type '{profile.root_type}'",
                subject=entry.path,
                evidence=[
                    EvidenceItem(
                        source=entry.path,
                        source_sha256=entry.sha256,
                        check="json.shape",
                        result={"root_type": profile.root_type, "keys": profile.keys},
                    )
                ],
            )

        if entry.format.format_id in _IDENTIFIER_SCAN_FORMATS:
            for hit in identifiers.scan_text_file(absolute, entry.path):
                identifier_hits.append(hit.as_dict())
                ledger.record(
                    f"a {hit.scheme.upper()} identifier '{hit.value}' appears in '{entry.path}'",
                    subject=entry.path,
                    evidence=[
                        EvidenceItem(
                            source=entry.path,
                            source_sha256=entry.sha256,
                            check="identifier.detected",
                            result=hit.value,
                            field=hit.scheme,
                            locator=f"line:{hit.line}",
                        )
                    ],
                )

    drift = _verify_source_unchanged(source, inventory, excludes)
    source_unchanged = drift is None
    if drift is not None:
        warnings.append(f"SOURCE CHANGED DURING INGEST: {drift}; this manifest is not trustworthy")

    manifest = _build_manifest(
        identity=identity,
        source=source,
        inventory=inventory,
        convention=active_convention,
        tables=tables,
        structured_docs=structured_docs,
        metadata_files=metadata_files,
        metadata_candidates=metadata_candidates,
        identifier_hits=identifier_hits,
        warnings=warnings,
    )

    provenance = ProvenanceRecord(
        dataset_id=identity,
        source_path=str(source),
        output_path=str(output),
        started_at=started_at,
        finished_at=utc_now(),
        duration_seconds=round(perf_counter() - started_monotonic, 3),
        tool=tool_fingerprint(),
        runtime=runtime_fingerprint(),
        configuration={
            "excludes": sorted(excludes),
            "manifest_version": MANIFEST_VERSION,
            "missing_value_convention": active_convention.as_dict(),
        },
    ).as_dict()
    provenance["source_verified_unchanged"] = source_unchanged

    result = IngestResult(identity, manifest, provenance, ledger, output)
    if write:
        _write_outputs(result)
    return result


def _build_manifest(
    *,
    identity: str,
    source: Path,
    inventory: Inventory,
    convention: MissingValueConvention,
    tables: dict[str, Any],
    structured_docs: dict[str, Any],
    metadata_files: list[dict[str, Any]],
    metadata_candidates: list[dict[str, Any]],
    identifier_hits: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    format_counts: dict[str, int] = {}
    for entry in inventory.files:
        format_counts[entry.format.format_id] = format_counts.get(entry.format.format_id, 0) + 1

    return {
        "manifest_version": MANIFEST_VERSION,
        "dataset_id": identity,
        # Only the directory's own name: an absolute path would make the manifest
        # machine-specific, and the manifest must not be.
        "source": {"root_name": source.name},
        # Declared at the top level because it governs how every missingness
        # number below should be read.
        "missing_value_convention": convention.as_dict(),
        "file_count": len(inventory.files),
        "total_bytes": inventory.total_bytes,
        "files": [entry.as_dict() for entry in inventory.files],
        "formats": dict(sorted(format_counts.items())),
        "tables": dict(sorted(tables.items())),
        "structured": dict(sorted(structured_docs.items())),
        "metadata_files": sorted(
            metadata_files, key=lambda item: (item["path"], item["convention"])
        ),
        # Files a recogniser applied to and could not finish reading. Kept apart
        # from metadata_files because a candidate is an open question, never a
        # finding: an empty metadata_files beside a non-empty list here means
        # "nothing was recognised, and these were never examined".
        "metadata_candidates": sorted(
            metadata_candidates, key=lambda item: (item["path"], item["reason"])
        ),
        "identifiers": sorted(
            identifier_hits, key=lambda item: (item["source"], item["scheme"], item["value"])
        ),
        # Cross-file relationships (a participants.tsv keyed to an observations
        # table, an ISA study/assay chain) require conventions this version does
        # not implement. An empty list here means "not determined", never "none".
        "relationships": [],
        "skipped": list(inventory.skipped),
        "warnings": sorted(set(warnings)),
    }


def _recognise_table(
    ledger: EvidenceLedger,
    entry: FileEntry,
    metadata_files: list[dict[str, Any]],
    *,
    path: str,
    rows: int,
    columns: list[tabular.ColumnProfile],
    locator: dict[str, Any] | None = None,
) -> None:
    """Offer one profiled table to the registry rule, and record what it said.

    The rule is handed counts only -- never cells -- so that no recognition can
    come to depend on what a value says. It answers ``None`` far more often than
    not, and that silence is the intended behaviour.
    """
    facts = [
        metadata.ColumnFacts(
            name=column.name,
            values=column.values,
            missing=column.missing,
            distinct=column.distinct,
            distinct_exact=column.distinct_exact,
            unique=column.unique,
        )
        for column in columns
    ]
    recognised = metadata.classify_table(
        path, file=entry.path, rows=rows, columns=facts, locator=locator
    )
    if recognised is not None:
        _record_content_metadata(ledger, entry, metadata_files, recognised)


def _record_content_metadata(
    ledger: EvidenceLedger,
    entry: FileEntry,
    metadata_files: list[dict[str, Any]],
    recognised: metadata.MetadataFile,
) -> None:
    """Record a content recognition, with the structure that produced it."""
    metadata_files.append(recognised.as_dict())
    ledger.record(
        f"'{recognised.path}' has the structure of {recognised.convention} metadata, "
        f"recognised from its content and not from its filename: {recognised.note}",
        subject=recognised.path,
        evidence=[
            EvidenceItem(
                source=entry.path,
                source_sha256=entry.sha256,
                check="metadata.content-signature",
                result=dict(recognised.basis),
                locator=recognised.path if recognised.path != entry.path else None,
            )
        ],
    )


def _record_candidate(
    ledger: EvidenceLedger,
    entry: FileEntry,
    metadata_candidates: list[dict[str, Any]],
    *,
    path: str,
    reason: str,
    note: str,
) -> None:
    """Record that a recogniser applied to a file and could not finish reading it."""
    candidate = metadata.MetadataCandidate(
        path=path, file=entry.path, reason=reason, note=note, filename_convention=None
    )
    metadata_candidates.append(candidate.as_dict())
    ledger.record(
        f"'{path}' could carry metadata that this version did not read: {note}",
        subject=path,
        evidence=[
            EvidenceItem(
                source=entry.path,
                source_sha256=entry.sha256,
                check="metadata.candidate",
                result={"reason": reason, "path": path},
            )
        ],
    )


def _record_dataset_claims(
    ledger: EvidenceLedger,
    identity: str,
    inventory: Inventory,
    convention: MissingValueConvention,
) -> None:
    ledger.record(
        f"the dataset contains {len(inventory.files)} file(s)",
        subject="dataset",
        evidence=[
            EvidenceItem(
                source="", source_sha256="", check="dataset.file-count", result=len(inventory.files)
            )
        ],
    )
    ledger.record(
        f"the dataset's content identity is {identity}",
        subject="dataset",
        evidence=[
            EvidenceItem(source="", source_sha256="", check="dataset.identity", result=identity)
        ],
    )


def _record_file_claims(ledger: EvidenceLedger, entry: FileEntry) -> None:
    ledger.record(
        f"'{entry.path}' has SHA-256 {entry.sha256}",
        subject=entry.path,
        evidence=[
            EvidenceItem(
                source=entry.path,
                source_sha256=entry.sha256,
                check="file.checksum",
                result=entry.sha256,
            )
        ],
    )
    ledger.record(
        f"'{entry.path}' is {entry.size} byte(s)",
        subject=entry.path,
        evidence=[
            EvidenceItem(
                source=entry.path, source_sha256=entry.sha256, check="file.size", result=entry.size
            )
        ],
    )
    ledger.record(
        f"'{entry.path}' was detected as format '{entry.format.format_id}' "
        f"by {entry.format.detected_by}",
        subject=entry.path,
        evidence=[
            EvidenceItem(
                source=entry.path,
                source_sha256=entry.sha256,
                check="file.format-detection",
                # The extension's claim travels with the verdict. A conflict that
                # appears only in the manifest cannot be substantiated through
                # get_evidence, which is where an agent has to look for it.
                result={
                    "format": entry.format.format_id,
                    "media_type": entry.format.media_type,
                    "detected_by": entry.format.detected_by,
                    **(
                        {
                            "extension_format": entry.format.extension_format,
                            "extension_conflict": entry.format.extension_conflict,
                        }
                        if entry.format.extension_format is not None
                        else {}
                    ),
                },
            )
        ],
    )


def _record_sheet_claims(
    ledger: EvidenceLedger,
    entry: FileEntry,
    sheet,
    convention: MissingValueConvention,
) -> None:
    """Record claims about one worksheet.

    Locators carry the workbook path *and* the sheet name, so an agent citing a
    cell-level fact can say where in the file it came from. A claim that names
    only the workbook is not checkable against a multi-sheet file (D2A-47).
    """
    convention_evidence = EvidenceItem(
        source="",
        source_sha256="",
        check="convention.missing-values",
        result=convention.as_dict(),
    )
    locator = {"workbook": sheet.workbook, "sheet": sheet.sheet, "header_row": sheet.header_row}

    if not sheet.profiled:
        # Content that could not be read has no row count. Asserting zero would
        # turn an inability to profile into a positive finding about the data.
        ledger.record(
            f"'{entry.path}' holds content that could not be profiled: "
            f"{'; '.join(sheet.warnings) or 'no reason recorded'}",
            subject=sheet.path,
            evidence=[
                EvidenceItem(
                    source=entry.path,
                    source_sha256=entry.sha256,
                    check="workbook.unprofiled",
                    result={"reason": sheet.warnings, **locator},
                )
            ],
        )
        return

    ledger.record(
        f"sheet '{sheet.sheet}' of '{entry.path}' has {sheet.rows} data row(s)",
        subject=sheet.path,
        evidence=[
            EvidenceItem(
                source=entry.path,
                source_sha256=entry.sha256,
                check="workbook.row-count",
                result={"rows": sheet.rows, **locator},
            )
        ],
    )

    for column in sheet.columns:
        if not column.missing:
            continue
        ledger.record(
            f"'{column.name}' is missing for {column.missing} of {sheet.rows} row(s) "
            f"in sheet '{sheet.sheet}' of '{entry.path}' "
            f"({column.missing_empty} empty, {column.missing_sentinel} resolved from "
            f"tokens by the '{convention.id}' convention)",
            subject=sheet.path,
            evidence=[
                EvidenceItem(
                    source=entry.path,
                    source_sha256=entry.sha256,
                    check="workbook.missing-value-count",
                    result={"column": column.name, "missing": column.missing, **locator},
                ),
                convention_evidence,
            ],
        )


def _record_table_claims(
    ledger: EvidenceLedger,
    entry: FileEntry,
    profile: tabular.TableProfile,
    convention: MissingValueConvention,
) -> None:
    # Cited alongside every missingness count, so a reader can always see which
    # rule produced the number rather than having to assume one.
    convention_evidence = EvidenceItem(
        source="",
        source_sha256="",
        check="convention.missing-values",
        result=convention.as_dict(),
    )

    ledger.record(
        f"'{entry.path}' has {profile.rows} data row(s)",
        subject=entry.path,
        evidence=[
            EvidenceItem(
                source=entry.path,
                source_sha256=entry.sha256,
                check="table.row-count",
                result=profile.rows,
            )
        ],
    )
    ledger.record(
        f"'{entry.path}' has {len(profile.columns)} column(s)",
        subject=entry.path,
        evidence=[
            EvidenceItem(
                source=entry.path,
                source_sha256=entry.sha256,
                check="table.column-list",
                result=[column.name for column in profile.columns],
            )
        ],
    )
    for column in profile.columns:
        ledger.record(
            f"column '{column.name}' in '{entry.path}' holds values shaped as '{column.dtype}'",
            subject=entry.path,
            evidence=[
                EvidenceItem(
                    source=entry.path,
                    source_sha256=entry.sha256,
                    check="table.column-dtype",
                    result=column.dtype,
                    field=column.name,
                    locator=f"column:{column.name}",
                )
            ],
        )
        if column.missing:
            ledger.record(
                f"'{column.name}' is missing for {column.missing} of {profile.rows} row(s) "
                f"in '{entry.path}' ({column.missing_empty} empty, {column.missing_sentinel} "
                f"resolved from tokens by the '{convention.id}' convention)",
                subject=entry.path,
                evidence=[
                    EvidenceItem(
                        source=entry.path,
                        source_sha256=entry.sha256,
                        check="table.missing-value-count",
                        result=column.missing,
                        field=column.name,
                        locator=f"column:{column.name}",
                    ),
                    EvidenceItem(
                        source=entry.path,
                        source_sha256=entry.sha256,
                        check="table.missing-empty-count",
                        result=column.missing_empty,
                        field=column.name,
                        locator=f"column:{column.name}",
                    ),
                    EvidenceItem(
                        source=entry.path,
                        source_sha256=entry.sha256,
                        check="table.missing-sentinel-count",
                        result=column.missing_sentinel,
                        field=column.name,
                        locator=f"column:{column.name}",
                    ),
                    convention_evidence,
                ],
            )
        if column.sentinel_tokens_seen:
            tokens = ", ".join(sorted(column.sentinel_tokens_seen))
            ledger.record(
                f"'{column.name}' in '{entry.path}' holds {column.missing_sentinel} cell(s) "
                f"with the token(s) {tokens}, resolved to missing by the "
                f"'{convention.id}' convention ({convention.source})",
                subject=entry.path,
                evidence=[
                    EvidenceItem(
                        source=entry.path,
                        source_sha256=entry.sha256,
                        check="table.missing-sentinel-count",
                        result=dict(sorted(column.sentinel_tokens_seen.items())),
                        field=column.name,
                        locator=f"column:{column.name}",
                    ),
                    convention_evidence,
                ],
            )
        if column.ambiguous_tokens_seen:
            total = sum(column.ambiguous_tokens_seen.values())
            tokens = ", ".join(sorted(column.ambiguous_tokens_seen))
            ledger.record(
                f"'{column.name}' in '{entry.path}' holds {total} cell(s) with the "
                f"token(s) {tokens}, which no convention resolves to missing; "
                f"whether they denote absence is undetermined",
                subject=entry.path,
                evidence=[
                    EvidenceItem(
                        source=entry.path,
                        source_sha256=entry.sha256,
                        check="table.ambiguous-token-count",
                        result=dict(sorted(column.ambiguous_tokens_seen.items())),
                        field=column.name,
                        locator=f"column:{column.name}",
                    ),
                    convention_evidence,
                ],
            )


def _discover_convention(source: Path, inventory: Inventory) -> MissingValueConvention:
    """Use the dataset's own declared missing-value convention when it makes one.

    A declared convention is evidence; the built-in default is a fallback, and
    the manifest says which of the two was used.
    """
    for entry in inventory.files:
        if entry.path.rsplit("/", 1)[-1].lower() != "datapackage.json":
            continue
        document, error = structured.load_json(source / entry.path)
        if error or not isinstance(document, dict):
            continue
        declared = conventions.from_datapackage(document, path=entry.path)
        if declared is not None:
            return declared
    return DEFAULT_CONVENTION


def _verify_source_unchanged(
    source: Path, inventory: Inventory, excludes: frozenset[str]
) -> str | None:
    """Prove the source is byte-for-byte what we inventoried. ``None`` means it is.

    Re-hashing the known entries is not sufficient on its own: a file created
    after the walk passed its directory is in neither the inventory nor the
    re-hash, so every checksum would agree while the manifest silently described
    less than the directory contains. The path set is therefore compared too --
    an addition or a removal is drift exactly as a content change is.
    """
    recorded = {entry.path for entry in inventory.files}
    current = {path.relative_to(source).as_posix() for path in walk(source, excludes, [], [])}

    if added := sorted(current - recorded):
        return f"{len(added)} file(s) appeared after the inventory was taken: {added}"
    if removed := sorted(recorded - current):
        return f"{len(removed)} inventoried file(s) disappeared during the run: {removed}"

    for entry in inventory.files:
        try:
            if hash_file(source / entry.path) != entry.sha256:
                return (
                    f"the checksum of '{entry.path}' differs from the value recorded at the start"
                )
        except OSError as error:
            return f"'{entry.path}' could not be re-read: {error}"
    return None


def _write_outputs(result: IngestResult) -> None:
    result.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(result.output_dir / MANIFEST_FILENAME, result.manifest)
    write_json(result.output_dir / PROVENANCE_FILENAME, result.provenance)
    write_json(result.output_dir / EVIDENCE_FILENAME, result.evidence.as_dict())


def write_json(path: Path, payload: Any) -> None:
    """Write JSON in one canonical style, so outputs diff cleanly."""
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8"
    )
