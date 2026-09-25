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
    ".xlsb": ("xlsb", "application/vnd.ms-excel.sheet.binary.macroEnabled.12"),
    ".ods": ("ods", "application/vnd.oasis.opendocument.spreadsheet"),
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
    # OLE2 Compound File Binary: the container of legacy BIFF .xls, .doc and
    # .ppt. Refined by its directory, exactly as a ZIP is by its member list.
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole2-container", "application/x-ole-storage"),
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
    # Deliberately empty: an OLE2 file whose directory shows no workbook stream
    # is not an .xls, and a name saying otherwise is a conflict worth reporting.
    "ole2-container": frozenset(),
    "xml": frozenset({"xml", "rdfxml"}),
    # NWB is HDF5, and MATLAB v7.3 is HDF5. An '.nwb' file over HDF5 bytes is
    # not a contradiction -- it is the specific name for the general container,
    # exactly like '.rdf' over XML. Without this entry those valid files were
    # reported as generic 'hdf5' with a spurious conflict.
    "hdf5": frozenset({"hdf5", "nwb", "matlab"}),
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
    # Binary OOXML: the same package, a binary workbook part instead of XML.
    ("xl/workbook.bin", "xlsb", "application/vnd.ms-excel.sheet.binary.macroEnabled.12"),
)

# OpenDocument declares its type in a stored 'mimetype' member, by specification
# the archive's first entry. Only the spreadsheet type is claimed.
_ODS_MIMETYPE = b"application/vnd.oasis.opendocument.spreadsheet"

# Root-level stream names that make an OLE2 compound file a BIFF workbook:
# 'Workbook' for BIFF8 (Excel 97-2003), 'Book' for BIFF5 (Excel 5/95).
_BIFF_STREAMS = frozenset({"Workbook", "Book"})
_OLE2_MAX_DIRECTORY_SECTORS = 64

# Formats whose contents this version profiles further.
TABULAR_FORMATS = frozenset({"csv", "tsv"})
# Tabular, but not delimited text: profiled by an optional reader (D2A-47).
WORKBOOK_FORMATS = frozenset({"xlsx", "xls", "xlsb", "ods"})
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
        elif observed.format_id == "ole2-container":
            refined = _refine_ole2_container(path)
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
    """Identify an OOXML or OpenDocument spreadsheet from the archive's members.

    Returns ``None`` for any ZIP that is not recognisably either, so a generic
    archive is never promoted on the strength of its magic bytes alone. Reads
    the central directory, plus the few bytes of an ODF 'mimetype' member.
    """
    import zipfile  # stdlib; the ingest core takes no third-party dependency

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "mimetype" in names and archive.getinfo("mimetype").file_size < 256:
                if archive.read("mimetype").strip() == _ODS_MIMETYPE:
                    return FormatInfo("ods", _ODS_MIMETYPE.decode("ascii"), "container")
    except (zipfile.BadZipFile, OSError, RuntimeError):
        # Truncated, unreadable or encrypted: the ZIP signature stands on its own.
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


def _refine_ole2_container(path: Path) -> FormatInfo | None:
    """Identify a BIFF workbook from an OLE2 compound file's directory.

    The file is read as [MS-CFB] lays it out: a 512-byte header, a FAT chaining
    sectors together, and a directory of 128-byte entries naming each stream.
    Only the directory is walked -- no stream is read -- and the walk is
    bounded, so a hostile or truncated file yields ``None`` rather than a hang.
    ``None`` leaves the file an ``ole2-container``: a guess is never made.
    """
    import struct

    try:
        with path.open("rb") as handle:
            header = handle.read(512)
            if len(header) < 512:
                return None
            sector_shift = struct.unpack_from("<H", header, 0x1E)[0]
            if sector_shift not in (9, 12):
                return None
            sector_size = 1 << sector_shift
            fat_sector_count = struct.unpack_from("<I", header, 0x2C)[0]
            directory_start = struct.unpack_from("<I", header, 0x30)[0]
            # The header carries the first 109 FAT sector locations: enough for
            # ~7 MB of 512-byte sectors or ~450 MB of 4 KB ones. Past that the
            # chain may end early, which only means "not identified".
            header_fat = struct.unpack_from("<109I", header, 0x4C)
            fat_locations = [
                value for value in header_fat[: min(fat_sector_count, 109)] if value < _CFB_MAX
            ]

            def read_sector(number: int) -> bytes:
                handle.seek((number + 1) * sector_size)
                return handle.read(sector_size)

            fat: list[int] = []
            for location in fat_locations:
                chunk = read_sector(location)
                fat.extend(struct.unpack_from(f"<{len(chunk) // 4}I", chunk))

            entries: list[bytes] = []
            sector, visited = directory_start, 0
            while sector < _CFB_MAX and visited < _OLE2_MAX_DIRECTORY_SECTORS:
                chunk = read_sector(sector)
                entries.extend(
                    chunk[offset : offset + 128] for offset in range(0, len(chunk) - 127, 128)
                )
                visited += 1
                sector = fat[sector] if sector < len(fat) else _CFB_MAX
    except (OSError, struct.error):
        return None

    # Only a stream directly under the root storage makes the file a workbook.
    # A Word or PowerPoint document embedding an Excel object carries a nested
    # 'Workbook' stream inside a child storage; scanning every entry would call
    # the whole outer document an .xls. The root's children form a tree linked
    # by left/right sibling ids, walked here without descending into storages.
    if not entries or entries[0][0x42] != 5:  # entry 0 must be the root storage
        return None
    pending = [struct.unpack_from("<I", entries[0], 0x4C)[0]]
    seen: set[int] = set()
    while pending:
        index = pending.pop()
        if index >= len(entries) or index in seen:
            continue  # NOSTREAM, out of range, or a cycle in a hostile file
        seen.add(index)
        entry = entries[index]
        pending.extend(struct.unpack_from("<2I", entry, 0x44))  # left, right siblings
        name_length = struct.unpack_from("<H", entry, 0x40)[0]
        if entry[0x42] != 2 or not 2 <= name_length <= 64:
            continue  # only a stream can hold a workbook
        name = entry[: name_length - 2].decode("utf-16-le", errors="replace")
        if name in _BIFF_STREAMS:
            return FormatInfo("xls", "application/vnd.ms-excel", "container")
    return None


# Sector numbers at or above this are [MS-CFB] markers (end of chain, free,
# FAT, DIFAT), never real sectors.
_CFB_MAX = 0xFFFFFFFA
