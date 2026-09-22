"""Format detection.

Detection is evidence-producing, not interpretive. A format is only claimed when
an extension or a magic-byte signature says so; anything else is reported as
``unknown`` and stays unknown. We never conclude "this is probably a table"
from a filename, and we never conclude anything about what the data *means*.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Extension -> (format_id, media_type). Deliberately small and explicit: an
# unlisted extension is a warning, not an invitation to guess.
_BY_EXTENSION: dict[str, tuple[str, str]] = {
    ".csv": ("csv", "text/csv"),
    ".tsv": ("tsv", "text/tab-separated-values"),
    ".tab": ("tsv", "text/tab-separated-values"),
    ".json": ("json", "application/json"),
    ".jsonld": ("jsonld", "application/ld+json"),
    ".yaml": ("yaml", "application/yaml"),
    ".yml": ("yaml", "application/yaml"),
    ".xml": ("xml", "application/xml"),
    ".ttl": ("turtle", "text/turtle"),
    ".nt": ("ntriples", "application/n-triples"),
    ".rdf": ("rdfxml", "application/rdf+xml"),
    ".md": ("markdown", "text/markdown"),
    ".txt": ("text", "text/plain"),
    ".rst": ("text", "text/plain"),
    ".png": ("png", "image/png"),
    ".jpg": ("jpeg", "image/jpeg"),
    ".jpeg": ("jpeg", "image/jpeg"),
    ".tif": ("tiff", "image/tiff"),
    ".tiff": ("tiff", "image/tiff"),
    ".pdf": ("pdf", "application/pdf"),
    ".zip": ("zip", "application/zip"),
    ".gz": ("gzip", "application/gzip"),
    ".parquet": ("parquet", "application/vnd.apache.parquet"),
    ".h5": ("hdf5", "application/x-hdf5"),
    ".hdf5": ("hdf5", "application/x-hdf5"),
    ".nwb": ("nwb", "application/x-hdf5"),
    ".mat": ("matlab", "application/x-matlab-data"),
    ".xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    # Listed so that a legacy name over OOXML bytes is a *disagreement* rather
    # than an absence of evidence. Without this entry there is no extension
    # claim to contradict, and the conflict goes unreported.
    ".xls": ("xls", "application/vnd.ms-excel"),
    ".nii": ("nifti", "application/x-nifti"),
    ".edf": ("edf", "application/x-edf"),
}

# Magic-byte signatures, checked against the file's first bytes. These outrank
# the extension when they disagree, because bytes are evidence and names are not.
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpeg", "image/jpeg"),
    (b"%PDF-", "pdf", "application/pdf"),
    (b"PK\x03\x04", "zip-container", "application/zip"),
    (b"\x1f\x8b", "gzip", "application/gzip"),
    (b"\x89HDF\r\n\x1a\n", "hdf5", "application/x-hdf5"),
    (b"PAR1", "parquet", "application/vnd.apache.parquet"),
    (b"SQLite format 3\x00", "sqlite", "application/vnd.sqlite3"),
    (b"<?xml", "xml", "application/xml"),
)

# Formats that are carriers rather than answers. A specific extension over one
# of these is usually the more useful of two true statements -- ".rdf" over XML
# bytes is RDF/XML, not merely XML -- so the extension is preferred, but only
# when the two are actually compatible. An incompatible pair is a conflict.
_CONTAINER_COMPATIBLE: dict[str, frozenset[str]] = {
    "zip-container": frozenset({"zip", "xlsx", "docx", "pptx"}),
    "xml": frozenset({"xml", "rdfxml"}),
}

# OOXML part paths that identify what a ZIP container actually holds. Checked
# against the archive's member list, so a plain .zip is never promoted merely
# for starting with the ZIP magic bytes.
_OOXML_MARKERS: tuple[tuple[str, str, str], ...] = (
    (
        "xl/workbook.xml",
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    (
        "word/document.xml",
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    (
        "ppt/presentation.xml",
        "pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
)

# Formats whose contents this version profiles further.
TABULAR_FORMATS = frozenset({"csv", "tsv"})
STRUCTURED_FORMATS = frozenset({"json", "jsonld"})
TEXT_FORMATS = frozenset({"markdown", "text", "yaml", "xml", "turtle", "ntriples", "rdfxml"})

_SIGNATURE_PEEK_BYTES = 16


@dataclass(frozen=True)
class FormatInfo:
    """What we can honestly say about a file's format, and on what basis.

    ``extension_format`` records what the *name* claimed, separately from what
    the bytes showed. Keeping both is the point: a file named ``.xls`` holding
    OOXML is not a detection failure, it is a finding, and a manifest that
    reports only the resolved format cannot express it.
    """

    format_id: str
    media_type: str
    detected_by: str  # "signature" | "extension" | "container" | "none"
    extension_format: str | None = None
    extension_conflict: bool = False

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "format": self.format_id,
            "media_type": self.media_type,
            "detected_by": self.detected_by,
        }
        # Emitted only when the name made a claim, so manifests for datasets
        # with no mis-extensioned files are unchanged.
        if self.extension_format is not None:
            payload["extension_format"] = self.extension_format
            payload["extension_conflict"] = self.extension_conflict
        return payload


UNKNOWN = FormatInfo("unknown", "application/octet-stream", "none")


def detect(path: Path) -> tuple[FormatInfo, list[str]]:
    """Detect a file's format from its bytes and its name.

    Returns the detection plus any notes worth surfacing as manifest warnings
    (an unrecognised extension, or bytes that contradict the name).
    """
    notes: list[str] = []
    extension = path.suffix.lower()
    by_extension = _BY_EXTENSION.get(extension)
    claimed = by_extension[0] if by_extension else None

    signature_hit = _match_signature(path)

    if signature_hit:
        observed = signature_hit

        # A carrier format can often be refined by looking inside it. This is
        # still evidence -- the archive's own member list -- not a guess from
        # the name, and a plain ZIP refines to nothing.
        if observed.format_id == "zip-container":
            refined = _refine_zip_container(path)
            if refined is not None:
                observed = refined

        compatible = _CONTAINER_COMPATIBLE.get(observed.format_id, frozenset())
        if claimed and claimed in compatible:
            # The name is the more specific of two true statements; ".rdf" over
            # XML bytes is RDF/XML. No conflict, and the extension wins.
            format_id, media_type = by_extension  # type: ignore[misc]
            return FormatInfo(format_id, media_type, "extension", claimed, False), notes

        if claimed and claimed != observed.format_id:
            notes.append(
                f"extension '{extension}' claims '{claimed}' but the file's content "
                f"is '{observed.format_id}'; reporting the content"
            )
            return (
                FormatInfo(
                    observed.format_id, observed.media_type, observed.detected_by, claimed, True
                ),
                notes,
            )

        return (
            FormatInfo(
                observed.format_id, observed.media_type, observed.detected_by, claimed, False
            ),
            notes,
        )

    if by_extension:
        format_id, media_type = by_extension
        return FormatInfo(format_id, media_type, "extension", claimed, False), notes

    notes.append(
        f"format not recognised for '{path.name}'"
        + (f" (extension '{extension}')" if extension else " (no extension)")
    )
    return UNKNOWN, notes


def _refine_zip_container(path: Path) -> FormatInfo | None:
    """Identify an OOXML document from the archive's member list.

    Returns ``None`` for any ZIP that is not recognisably OOXML, so a generic
    archive is never promoted on the strength of its magic bytes alone. Reads
    the central directory only -- no member is decompressed.
    """
    import zipfile  # stdlib; the ingest core takes no third-party dependency

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        # Truncated or unreadable: the ZIP signature still stands on its own.
        return None

    if "[Content_Types].xml" not in names:
        return None
    for marker, format_id, media_type in _OOXML_MARKERS:
        if marker in names:
            return FormatInfo(format_id, media_type, "container")
    return None


def _match_signature(path: Path) -> FormatInfo | None:
    try:
        with path.open("rb") as handle:
            head = handle.read(_SIGNATURE_PEEK_BYTES)
    except OSError:
        return None
    for magic, format_id, media_type in _SIGNATURES:
        if head.startswith(magic):
            return FormatInfo(format_id, media_type, "signature")
    return None
