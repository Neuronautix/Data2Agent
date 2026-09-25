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
from ..errors import LayoutError, OutputError
from ..evidence import EvidenceItem, EvidenceLedger
from . import boris, conventions, formats, identifiers, metadata, structured, tabular
from .checksum import dataset_id as fold_dataset_id
from .checksum import hash_file
from .conventions import DEFAULT_CONVENTION, MissingValueConvention
from .inventory import DEFAULT_EXCLUDES, FileEntry, Inventory, build, walk
from .layout import HeaderLayout, LayoutDeclarations, TableBlocks
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
    layouts: LayoutDeclarations | None = None,
    write: bool = True,
) -> IngestResult:
    """Ingest ``source`` into ``output``, leaving ``source`` untouched.

    ``convention`` overrides how missing-value tokens are resolved. Left unset,
    the dataset's own declaration is used when it makes one, and the built-in
    default otherwise -- in every case the choice is recorded in the manifest
    and cited by each missingness claim.

    ``layouts`` declares where named tables' headers are (D2A-97). A declared
    table path that matches no profiled table raises :class:`LayoutError`
    before anything is written: a declaration that silently applied to nothing
    would leave a user believing a header was fixed when it was not.
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
    # Workbook parsers actually used, name -> installed version. Run-specific,
    # so it goes to provenance: the manifest names the backend per sheet but
    # never a version, which would break its byte-identity across installs.
    workbook_readers: dict[str, str | None] = {}
    # Declared table paths whose profile was actually produced under the
    # declared layout, in THIS ingest. Tracked here rather than on the
    # declarations object, which is immutable and may be reused across ingests.
    applied_layouts: set[str] = set()

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
            declared = layouts.for_table(entry.path) if layouts else None
            if isinstance(declared, TableBlocks):
                profiles = tabular.profile_table_blocks(
                    absolute, entry.path, active_convention, declared
                )
            else:
                single = tabular.profile_table(absolute, entry.path, active_convention, declared)
                profiles = None if single is None else [single]
            if profiles is None:
                warnings.append(f"{entry.path}: could not be read as a delimited table")
            for profile in profiles or []:
                tables[profile.path] = profile.as_dict()
                if _declared_layout_applied(profile.layout):
                    applied_layouts.add(_declared_path(profile.path, profile.block))
                warnings.extend(f"{profile.path}: {note}" for note in profile.warnings)
                _record_table_claims(ledger, entry, profile, active_convention)
                _record_header_claim(ledger, entry, profile.path, profile.layout, layouts)
                _record_block_claim(ledger, entry, profile.path, profile.block)
                if classification is None:
                    _recognise_table(
                        ledger,
                        entry,
                        metadata_files,
                        path=profile.path,
                        rows=profile.rows,
                        columns=profile.columns,
                    )

        elif entry.format.format_id in formats.WORKBOOK_FORMATS:
            # Reached only after the format was established from the file's
            # bytes, so a workbook named '.xls' arrives here too (D2A-46).
            from ..readers import workbook as workbook_reader

            backend = workbook_reader.backend_for(entry.format.format_id)
            if not backend.available():
                # The core stays honest about what it cannot see. A workbook the
                # tool could not open is reported as exactly that -- never folded
                # into silence, which is what made this gap invisible before.
                warnings.append(
                    f"{entry.path}: identified as a '{entry.format.format_id}' workbook but "
                    f"not profiled; install the '{backend.extra}' extra to enable it"
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
                            f"'{backend.extra}' reader is not installed; whether it carries "
                            "metadata is undetermined"
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
                            result={
                                "format": entry.format.format_id,
                                "extra": backend.extra,
                                "backend": backend.name,
                            },
                        )
                    ],
                )
            else:
                workbook_readers[backend.name] = backend.version()
                for sheet in workbook_reader.profile_workbook(
                    absolute,
                    entry.path,
                    active_convention,
                    entry.format.format_id,
                    layout_for=layouts.for_table if layouts else None,
                ):
                    tables[sheet.path] = sheet.as_dict()
                    if sheet.profiled and _declared_layout_applied(sheet.layout):
                        applied_layouts.add(_declared_path(sheet.path, sheet.block))
                    warnings.extend(f"{sheet.path}: {note}" for note in sheet.warnings)
                    _record_sheet_claims(ledger, entry, sheet, active_convention)
                    _record_header_claim(
                        ledger,
                        entry,
                        sheet.path,
                        sheet.layout,
                        layouts,
                        sheet={"workbook": sheet.workbook, "sheet": sheet.sheet},
                    )
                    _record_block_claim(ledger, entry, sheet.path, sheet.block)
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
            profile = structured.profile_document(
                document,
                parse_error,
                relative_path=entry.path,
                boris=entry.format.format_id == "boris",
            )
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
            if entry.format.format_id == "boris" and profile.parse_error is None:
                # A BORIS project's events are observations, so they are tables
                # (D2A-109): profiled here, read from the verified file on demand.
                for derived in boris.profile_project(document, entry.path, active_convention):
                    tables[derived.path] = derived.as_dict()
                    warnings.extend(f"{derived.path}: {note}" for note in derived.warnings)
                    _record_boris_table_claims(ledger, entry, derived, active_convention)
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

    if layouts is not None and (unapplied := layouts.unapplied(applied_layouts)):
        raise LayoutError(
            f"layout declaration {layouts.name!r} names {len(unapplied)} table(s) that this "
            f"ingest could not apply it to: {unapplied}. Either no such table exists -- "
            f"table paths are '<file>' for a delimited file and '<workbook>#<sheet>' for a "
            f"worksheet, exactly as manifest.tables keys them -- or the file could not be "
            f"profiled as a table. Profiled tables: {sorted(tables)}"
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
        layouts=layouts,
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
            "layout_declaration": layouts.provenance_record() if layouts else None,
            "workbook_readers": dict(sorted(workbook_readers.items())),
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
    layouts: LayoutDeclarations | None,
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
        # Like the convention, a layout declaration governs how the tables below
        # were read, so its digest is part of what the manifest says: a re-ingest
        # under another declaration is another manifest, and a relationships
        # sidecar bound to the old one is refused.
        "layout_declaration": layouts.manifest_record() if layouts else None,
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


def _declared_path(table_path: str, block: dict[str, Any] | None) -> str:
    """The declaration key a table answers to: its parent's, for a declared block."""
    return str(block["parent_table"]) if block else table_path


def _record_block_claim(
    ledger: EvidenceLedger,
    entry: FileEntry,
    table_path: str,
    block: dict[str, Any] | None,
) -> None:
    """Record that a table is one declared block of a larger sheet or file (D2A-103).

    A block's rows are a slice of its parent's; without this claim an agent has
    no citable fact saying where the slice starts and ends, or that a person --
    not a rule -- drew its boundaries.
    """
    if not block:
        return
    columns = f", columns {block['columns']}" if block.get("columns") else ""
    ledger.record(
        f"'{table_path}' is declared block '{block['name']}' of '{block['parent_table']}', "
        f"header on row {block['header_row']}, rows through {block['last_row']}{columns}",
        subject=table_path,
        evidence=[
            EvidenceItem(
                source=entry.path,
                source_sha256=entry.sha256,
                check="layout.block",
                result=dict(block),
                locator=f"rows:{block['first_row']}-{block['last_row']}",
            )
        ],
    )


def _declared_layout_applied(layout: HeaderLayout | None) -> bool:
    """Whether a table profile was produced under a declared layout."""
    return layout is not None and layout.declared is not None


_HOW_CHOSEN = {
    "first-non-empty": "the first non-empty row",
    "detected": "detected under the named header rule",
    "declared": "declared by a layout declaration",
}


def _record_header_claim(
    ledger: EvidenceLedger,
    entry: FileEntry,
    table_path: str,
    layout: HeaderLayout | None,
    layouts: LayoutDeclarations | None,
    *,
    sheet: dict[str, Any] | None = None,
) -> None:
    """Record which row was taken as the header, by what, and what was skipped.

    Every column name in the manifest rests on this choice, so it is a claim of
    its own: an agent asked why a column is called what it is can cite it.
    """
    if layout is None or layout.header_row is None:
        return
    fields = layout.as_dict()
    skipped = fields["header_detection"]["skipped_rows"]
    span = (
        f"row {layout.header_row}"
        if layout.header_rows == 1
        else f"rows {layout.header_row}-{layout.header_row + layout.header_rows - 1}"
    )
    statement = f"the header of '{table_path}' is {span}, {_HOW_CHOSEN[fields['header_source']]}"
    if skipped:
        statement += f"; row(s) {', '.join(str(item['row']) for item in skipped)} skipped"
    # A sheet's row is a spreadsheet row; a delimited file's is the line on
    # which the header record starts.
    unit = "row" if sheet is not None else "line"
    evidence = [
        EvidenceItem(
            source=entry.path,
            source_sha256=entry.sha256,
            check="table.header-layout",
            result={**(sheet or {}), **fields},
            locator=f"{unit}:{layout.header_row}",
        )
    ]
    if fields["header_source"] == "declared" and layouts is not None:
        evidence.append(
            EvidenceItem(
                source="",
                source_sha256="",
                check="layout.declaration",
                result=layouts.manifest_record(),
            )
        )
    ledger.record(statement, subject=table_path, evidence=evidence)


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


def _record_boris_table_claims(
    ledger: EvidenceLedger,
    entry: FileEntry,
    profile: boris.BorisTableProfile,
    convention: MissingValueConvention,
) -> None:
    """Claims for one table derived from a BORIS project.

    A profiled table gets exactly the claims a delimited table gets. One that
    could not be derived gets a claim saying so and why -- never a row count of
    zero, which would turn an unread shape into a finding about the scoring.
    """
    if not profile.profiled:
        ledger.record(
            f"'{profile.path}' could not be derived from '{entry.path}': "
            f"{'; '.join(profile.warnings) or 'no reason recorded'}",
            subject=profile.path,
            evidence=[
                EvidenceItem(
                    source=entry.path,
                    source_sha256=entry.sha256,
                    check="boris.unprofiled",
                    result={"table": profile.table, "reason": profile.warnings},
                )
            ],
        )
        return
    _record_table_claims(ledger, entry, profile, convention)
    if profile.pairing is not None:
        ledger.record(
            f"'{profile.path}' pairs state events under BORIS's toggle rule: "
            + ", ".join(f"{count} {outcome}" for outcome, count in profile.pairing.items()),
            subject=profile.path,
            evidence=[
                EvidenceItem(
                    source=entry.path,
                    source_sha256=entry.sha256,
                    check="boris.interval-pairing",
                    result=dict(profile.pairing),
                )
            ],
        )


def _record_table_claims(
    ledger: EvidenceLedger,
    entry: FileEntry,
    profile: tabular.TableProfile | boris.BorisTableProfile,
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
        f"'{profile.path}' has {profile.rows} data row(s)",
        subject=profile.path,
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
        f"'{profile.path}' has {len(profile.columns)} column(s)",
        subject=profile.path,
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
            f"column '{column.name}' in '{profile.path}' holds values shaped as '{column.dtype}'",
            subject=profile.path,
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
                f"in '{profile.path}' ({column.missing_empty} empty, {column.missing_sentinel} "
                f"resolved from tokens by the '{convention.id}' convention)",
                subject=profile.path,
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
                f"'{column.name}' in '{profile.path}' holds {column.missing_sentinel} cell(s) "
                f"with the token(s) {tokens}, resolved to missing by the "
                f"'{convention.id}' convention ({convention.source})",
                subject=profile.path,
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
                f"'{column.name}' in '{profile.path}' holds {total} cell(s) with the "
                f"token(s) {tokens}, which no convention resolves to missing; "
                f"whether they denote absence is undetermined",
                subject=profile.path,
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
