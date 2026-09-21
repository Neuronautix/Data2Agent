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
from typing import Any

from .. import MANIFEST_VERSION
from ..errors import OutputError
from ..evidence import EvidenceItem, EvidenceLedger
from . import formats, identifiers, metadata, structured, tabular
from .checksum import dataset_id as fold_dataset_id
from .checksum import hash_file
from .inventory import DEFAULT_EXCLUDES, FileEntry, Inventory, build
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
    write: bool = True,
) -> IngestResult:
    """Ingest ``source`` into ``output``, leaving ``source`` untouched."""
    started_at = utc_now()
    source = Path(source).expanduser().resolve()
    output = Path(output).expanduser().resolve()

    if output == source or source in output.parents:
        raise OutputError(
            "output directory must sit outside the dataset source, so that ingestion "
            f"cannot alter what it is describing: {output}"
        )

    inventory = build(source, excludes=excludes)
    identity = fold_dataset_id((entry.path, entry.sha256) for entry in inventory.files)

    ledger = EvidenceLedger(identity)
    warnings = list(inventory.warnings)

    tables: dict[str, Any] = {}
    structured_docs: dict[str, Any] = {}
    metadata_files: list[dict[str, str]] = []
    identifier_hits: list[dict[str, Any]] = []

    _record_dataset_claims(ledger, identity, inventory)

    for entry in inventory.files:
        absolute = source / entry.path
        _record_file_claims(ledger, entry)

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
            profile = tabular.profile_table(absolute, entry.path)
            if profile is None:
                warnings.append(f"{entry.path}: could not be read as a delimited table")
            else:
                tables[entry.path] = profile.as_dict()
                warnings.extend(f"{entry.path}: {note}" for note in profile.warnings)
                _record_table_claims(ledger, entry, profile)

        elif entry.format.format_id in formats.STRUCTURED_FORMATS:
            profile = structured.profile_json(absolute, entry.path)
            structured_docs[entry.path] = profile.as_dict()
            if profile.parse_error:
                warnings.append(f"{entry.path}: JSON parse error: {profile.parse_error}")
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

    source_unchanged = _verify_source_unchanged(source, inventory)
    if not source_unchanged:
        warnings.append(
            "SOURCE CHANGED DURING INGEST: at least one file's checksum differs from "
            "the value recorded at the start of the run; this manifest is not trustworthy"
        )

    manifest = _build_manifest(
        identity=identity,
        source=source,
        inventory=inventory,
        tables=tables,
        structured_docs=structured_docs,
        metadata_files=metadata_files,
        identifier_hits=identifier_hits,
        warnings=warnings,
    )

    provenance = ProvenanceRecord(
        dataset_id=identity,
        source_path=str(source),
        output_path=str(output),
        started_at=started_at,
        finished_at=utc_now(),
        tool=tool_fingerprint(),
        runtime=runtime_fingerprint(),
        configuration={"excludes": sorted(excludes), "manifest_version": MANIFEST_VERSION},
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
    tables: dict[str, Any],
    structured_docs: dict[str, Any],
    metadata_files: list[dict[str, str]],
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
        "file_count": len(inventory.files),
        "total_bytes": inventory.total_bytes,
        "files": [entry.as_dict() for entry in inventory.files],
        "formats": dict(sorted(format_counts.items())),
        "tables": dict(sorted(tables.items())),
        "structured": dict(sorted(structured_docs.items())),
        "metadata_files": sorted(metadata_files, key=lambda item: item["path"]),
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


def _record_dataset_claims(ledger: EvidenceLedger, identity: str, inventory: Inventory) -> None:
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
                result={
                    "format": entry.format.format_id,
                    "media_type": entry.format.media_type,
                    "detected_by": entry.format.detected_by,
                },
            )
        ],
    )


def _record_table_claims(
    ledger: EvidenceLedger, entry: FileEntry, profile: tabular.TableProfile
) -> None:
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
                f"in '{entry.path}'",
                subject=entry.path,
                evidence=[
                    EvidenceItem(
                        source=entry.path,
                        source_sha256=entry.sha256,
                        check="table.missing-value-count",
                        result=column.missing,
                        field=column.name,
                        locator=f"column:{column.name}",
                    )
                ],
            )
        if column.null_like:
            ledger.record(
                f"'{column.name}' in '{entry.path}' holds {column.null_like} null-like token(s); "
                f"whether they denote missing values is undetermined",
                subject=entry.path,
                evidence=[
                    EvidenceItem(
                        source=entry.path,
                        source_sha256=entry.sha256,
                        check="table.null-like-token-count",
                        result=column.null_like,
                        field=column.name,
                        locator=f"column:{column.name}",
                    )
                ],
            )


def _verify_source_unchanged(source: Path, inventory: Inventory) -> bool:
    """Re-checksum every file to prove ingestion did not touch the source."""
    for entry in inventory.files:
        try:
            if hash_file(source / entry.path) != entry.sha256:
                return False
        except OSError:
            return False
    return True


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
