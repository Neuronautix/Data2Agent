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
    metadata_files: list[dict[str, str]] = []
    identifier_hits: list[dict[str, Any]] = []

    _record_dataset_claims(ledger, identity, inventory, active_convention)

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
            profile = tabular.profile_table(absolute, entry.path, active_convention)
            if profile is None:
                warnings.append(f"{entry.path}: could not be read as a delimited table")
            else:
                tables[entry.path] = profile.as_dict()
                warnings.extend(f"{entry.path}: {note}" for note in profile.warnings)
                _record_table_claims(ledger, entry, profile, active_convention)

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
        # Declared at the top level because it governs how every missingness
        # number below should be read.
        "missing_value_convention": convention.as_dict(),
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
                result={
                    "format": entry.format.format_id,
                    "media_type": entry.format.media_type,
                    "detected_by": entry.format.detected_by,
                },
            )
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
