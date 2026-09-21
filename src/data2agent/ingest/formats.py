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
)

# Formats whose contents this version profiles further.
TABULAR_FORMATS = frozenset({"csv", "tsv"})
STRUCTURED_FORMATS = frozenset({"json", "jsonld"})
TEXT_FORMATS = frozenset({"markdown", "text", "yaml", "xml", "turtle", "ntriples", "rdfxml"})

_SIGNATURE_PEEK_BYTES = 16


@dataclass(frozen=True)
class FormatInfo:
    """What we can honestly say about a file's format, and on what basis."""

    format_id: str
    media_type: str
    detected_by: str  # "signature" | "extension" | "none"

    def as_dict(self) -> dict[str, str]:
        return {
            "format": self.format_id,
            "media_type": self.media_type,
            "detected_by": self.detected_by,
        }


UNKNOWN = FormatInfo("unknown", "application/octet-stream", "none")


def detect(path: Path) -> tuple[FormatInfo, list[str]]:
    """Detect a file's format from its bytes and its name.

    Returns the detection plus any notes worth surfacing as manifest warnings
    (an unrecognised extension, or bytes that contradict the name).
    """
    notes: list[str] = []
    extension = path.suffix.lower()
    by_extension = _BY_EXTENSION.get(extension)

    signature_hit = _match_signature(path)

    # A ZIP container is the carrier for .xlsx and friends; when the extension
    # names the specific format, that is the more useful of two true answers.
    if signature_hit and signature_hit.format_id == "zip-container" and by_extension:
        signature_hit = None

    if signature_hit:
        if by_extension and by_extension[0] != signature_hit.format_id:
            notes.append(
                f"extension '{extension}' suggests '{by_extension[0]}' but the file's "
                f"leading bytes match '{signature_hit.format_id}'; reporting the bytes"
            )
        return signature_hit, notes

    if by_extension:
        format_id, media_type = by_extension
        return FormatInfo(format_id, media_type, "extension"), notes

    notes.append(
        f"format not recognised for '{path.name}'"
        + (f" (extension '{extension}')" if extension else " (no extension)")
    )
    return UNKNOWN, notes


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
