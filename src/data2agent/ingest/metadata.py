"""Metadata recognition: by filename convention, and by content structure.

Two rules run, in this order, and a file is recognised by at most one of them.

**By filename** (v0.1). The *name* matches a published convention (RO-Crate,
ISA-Tab, BIDS, DataCite, Frictionless, a README). The classification says "this
file is conventionally metadata", nothing more: not that it is valid, complete or
even parseable.

**By content** (D2A-49a). The filename matched nothing, but the file's *structure*
is that of a metadata record. Two signatures, both deliberately narrow:

``json-standard``
    A JSON object that names a published metadata standard in its own content --
    an ``@context`` pointing at a known vocabulary, a ``BIDSVersion``, a
    Frictionless ``resources`` list, a DataCite record shape. This is the same
    set of standards the filename rule knows, recognised by what the document
    says about itself instead of by what it is called.
``keyed-registry-table``
    A table (a CSV, or one worksheet of a workbook) with exactly one column named
    for a subject identifier, whose values are complete and unique across every
    row, where every other column takes at most half as many distinct values as
    there are rows. One row per subject, plus grouping attributes: the structure
    of a registry rather than of a measurement table.

Both claims are **structural**. Neither says what the file means. A registry
table is not read as "these are the animals in the experiment"; it is read as
"one row per distinct '<column>', with N grouping columns". Interpreting that is
the semantic layer's job (#2), and doing it here would be exactly the inference
this project refuses to make.

Why this narrow. A false positive is worse than a false negative, because five
FAIR indicators cascade from ``metadata_files`` and a data table wrongly called
metadata corrupts all five at once. Every condition below is one a data table
routinely fails: a measurement table repeats its subject id across rows (so the
key is not unique), and a continuous measurement individuates rows rather than
grouping them (so the ratio test fails). What is missed instead -- a registry
whose attribute columns are too varied, a descriptor that declares no standard --
is reported as nothing found, which is the honest direction to be wrong in.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# (regex over the lower-cased filename, convention, note)
_CONVENTIONS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"^ro-crate-metadata\.jsonl?d?$|^ro-crate-metadata\.json$"),
        "ro-crate",
        "RO-Crate root metadata",
    ),
    (re.compile(r"^datapackage\.json$"), "frictionless", "Frictionless Data Package descriptor"),
    (re.compile(r"^dataset_description\.json$"), "bids", "BIDS dataset description"),
    (re.compile(r"^participants\.(tsv|json)$"), "bids", "BIDS participants file"),
    (re.compile(r"^(i|s|a)_.*\.txt$"), "isa-tab", "ISA-Tab investigation/study/assay file"),
    (re.compile(r"^codemeta\.json$"), "codemeta", "CodeMeta descriptor"),
    (re.compile(r"^datacite\.(json|xml)$"), "datacite", "DataCite metadata"),
    (re.compile(r"^metadata\.(json|yaml|yml|xml)$"), "generic", "Generic metadata document"),
    (re.compile(r"^readme(\.md|\.txt|\.rst)?$"), "readme", "Human-readable dataset description"),
    (re.compile(r"^license(\.md|\.txt)?$|^licence(\.md|\.txt)?$"), "license", "Licence statement"),
    (re.compile(r"^changelog(\.md|\.txt)?$"), "changelog", "Change log"),
)

# How a file came to be recognised. Recorded on every entry, so a consumer can
# weigh a filename match differently from a structural one if it wants to.
BY_FILENAME = "filename_convention"
BY_CONTENT = "content"

# What the file is to the dataset.
#
# ``document``  the file's content *is* the metadata record (a README, a
#               descriptor). It is not part of the dataset's payload.
# ``embedded``  metadata carried inside a file that is also payload -- a registry
#               worksheet in a workbook of results. Kept distinct because such a
#               file must still count as a data file: a misfired recognition can
#               then never remove a data file from the format and linkage checks.
DOCUMENT = "document"
EMBEDDED = "embedded"

# Column names that denote the individual a row is about. Matched exactly, after
# normalisation -- never as a suffix. '*_id' as a pattern would match
# 'observation_id' and 'session_id', which key events, not subjects, and a table
# of events is a measurement table however unique its key.
SUBJECT_IDENTIFIER_NAMES = frozenset(
    {
        "id",
        "ids",
        "animal",
        "animal_id",
        "animal_number",
        "subject",
        "subject_id",
        "participant",
        "participant_id",
        "mouse",
        "mouse_id",
        "rat",
        "rat_id",
        "individual",
        "individual_id",
        "specimen",
        "specimen_id",
        "donor",
        "donor_id",
        "sample",
        "sample_id",
    }
)

# A registry needs enough rows for "distinct values per row" to mean anything.
_MIN_REGISTRY_ROWS = 4

# JSON-LD @context values that name a metadata vocabulary, longest-lived first.
_CONTEXT_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("w3id.org/ro/crate", "ro-crate", "RO-Crate metadata, declared by its own @context"),
    (
        "doi.org/10.5063/schema/codemeta",
        "codemeta",
        "CodeMeta descriptor, declared by its own @context",
    ),
    ("schema.org", "schema-org", "JSON-LD document in the schema.org vocabulary"),
    ("purl.org/dc/", "dublin-core", "JSON-LD document in the Dublin Core vocabulary"),
)

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ColumnFacts:
    """The structural facts about one column that the table rule may read.

    A deliberately small surface. The rule is given counts and a uniqueness
    verdict -- never the cells themselves -- so it cannot come to depend on what
    any value says.
    """

    name: str
    values: int
    missing: int
    distinct: int
    distinct_exact: bool
    unique: bool | None  # None: not determined (too many values to track)


@dataclass(frozen=True)
class MetadataFile:
    """A file, or a part of one, recognised as carrying metadata."""

    path: str  # the file, or '<workbook path>#<sheet name>' for one worksheet
    convention: str
    note: str
    recognised_by: str = BY_FILENAME
    # What the filename rule said, whichever rule actually fired. Kept visible
    # so "recognised by content" never hides "and the name matched nothing".
    filename_convention: str | None = None
    kind: str = DOCUMENT
    file: str = ""  # the dataset file carrying it; equals ``path`` for whole files
    basis: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "convention": self.convention,
            "note": self.note,
            "recognised_by": self.recognised_by,
            "filename_convention": self.filename_convention,
            "kind": self.kind,
            "file": self.file or self.path,
            "basis": dict(self.basis),
        }


@dataclass(frozen=True)
class MetadataCandidate:
    """A file a recogniser applied to and could not finish reading.

    Emitted so that "we looked and found nothing" stays distinguishable from "we
    could not look". A candidate is never counted as metadata: it is the record
    of an open question, and the FAIR checks cite it as one.
    """

    path: str
    file: str
    reason: str  # machine-readable: 'reader-unavailable' | 'unprofiled' | 'unparsed'
    note: str
    filename_convention: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "file": self.file or self.path,
            "reason": self.reason,
            "note": self.note,
            "filename_convention": self.filename_convention,
        }


def classify(relative_path: str) -> MetadataFile | None:
    """Return a classification when the filename matches a known convention."""
    name = relative_path.rsplit("/", 1)[-1].lower()
    for pattern, convention, note in _CONVENTIONS:
        if pattern.match(name):
            return MetadataFile(
                path=relative_path,
                convention=convention,
                note=note,
                recognised_by=BY_FILENAME,
                filename_convention=convention,
                kind=DOCUMENT,
                file=relative_path,
                basis={"signature": "filename-convention", "matched": name},
            )
    return None


def normalise_column_name(name: str) -> str:
    """Lower-case, and collapse every run of non-alphanumerics to one underscore."""
    return _NON_ALPHANUMERIC.sub("_", name.strip().lower()).strip("_")


def is_subject_identifier_name(name: str) -> bool:
    """Whether a column name is one of the closed set of subject-identifier names."""
    return normalise_column_name(name) in SUBJECT_IDENTIFIER_NAMES


def classify_json_document(relative_path: str, document: Any) -> MetadataFile | None:
    """Recognise a JSON document that names a published metadata standard.

    The document has to declare the standard itself -- a vocabulary in
    ``@context``, a ``BIDSVersion``, a Frictionless ``resources`` list, a DataCite
    record shape. A bag of plausible-looking keys is not enough: ``name``,
    ``description`` and ``version`` describe a software package as readily as a
    dataset, and guessing between the two is the kind of inference that would
    make five FAIR verdicts unfalsifiable.
    """
    if not isinstance(document, dict):
        return None

    by_lowered = {key.lower(): key for key in document if isinstance(key, str)}

    context = document.get(by_lowered.get("@context", "@context"))
    if context is not None:
        flattened = _flatten_context(context)
        for marker, convention, note in _CONTEXT_MARKERS:
            if any(marker in value for value in flattened):
                return _content_document(
                    relative_path,
                    convention,
                    note,
                    {"signature": "json-standard", "declared_by": "@context", "marker": marker},
                )

    bids_key = by_lowered.get("bidsversion")
    if bids_key is not None and isinstance(document[bids_key], str):
        return _content_document(
            relative_path,
            "bids",
            "BIDS dataset description, declared by its own BIDSVersion",
            {"signature": "json-standard", "declared_by": bids_key},
        )

    resources = document.get(by_lowered.get("resources", "resources"))
    if _is_frictionless_resources(resources):
        return _content_document(
            relative_path,
            "frictionless",
            "Frictionless descriptor: a 'resources' list of path-bearing objects",
            {"signature": "json-standard", "declared_by": "resources"},
        )

    if _is_datacite_record(document, by_lowered):
        return _content_document(
            relative_path,
            "datacite",
            "DataCite record shape: a resource type, or titles and creators",
            {"signature": "json-standard", "declared_by": "datacite-shape"},
        )

    return None


def classify_table(
    path: str,
    *,
    file: str,
    rows: int,
    columns: Sequence[ColumnFacts],
    locator: dict[str, Any] | None = None,
) -> MetadataFile | None:
    """Recognise a table whose structure is that of a subject registry.

    Every condition is checked against observed counts, and each one is a
    condition an ordinary data table fails:

    1. at least four rows -- below that a ratio of distinct values to rows says
       nothing;
    2. exactly one column named for a subject identifier -- two of them is a
       join or a measurement table, zero is any table at all;
    3. that column is complete and unique across every row, so the table holds
       one row per subject rather than repeated observations of one;
    4. at least one other column, and every other column takes at most half as
       many distinct values as there are rows -- it groups the subjects rather
       than individuating them, which is what an attribute does and what a
       measurement does not;
    5. at least one of those columns takes two or more distinct values, so the
       table records an attribute rather than a bare list of identifiers.

    Condition 4 reads ``distinct`` even where it is an upper bound: an upper
    bound below half the row count still *proves* the true count is below it.
    """
    if rows < _MIN_REGISTRY_ROWS or not columns:
        return None

    key_positions = [
        index for index, column in enumerate(columns) if is_subject_identifier_name(column.name)
    ]
    if len(key_positions) != 1:
        return None

    key = columns[key_positions[0]]
    # Unique *and* complete: a key with a blank cell does not identify that row,
    # and 'unique is None' means uniqueness was never established -- which is not
    # the same as established false, and is equally not grounds for a claim.
    if key.unique is not True or key.missing != 0 or key.values != rows:
        return None

    others = [column for index, column in enumerate(columns) if index != key_positions[0]]
    if not others:
        return None
    if not all(column.distinct * 2 <= rows for column in others):
        return None
    if not any(column.distinct >= 2 for column in others):
        return None

    grouping = [column.name for column in others]
    basis: dict[str, Any] = {
        "signature": "keyed-registry-table",
        "key_column": key.name,
        "rows": rows,
        "grouping_columns": grouping,
    }
    if locator:
        basis.update(locator)

    return MetadataFile(
        path=path,
        convention="keyed-registry-table",
        note=(
            f"one row per distinct '{key.name}' across {rows} row(s), with "
            f"{len(grouping)} grouping column(s): {', '.join(grouping)}. A structural "
            f"reading only -- what the rows denote is not determined here"
        ),
        recognised_by=BY_CONTENT,
        filename_convention=None,
        kind=EMBEDDED,
        file=file,
        basis=basis,
    )


# -- helpers ---------------------------------------------------------------


def _content_document(
    relative_path: str, convention: str, note: str, basis: dict[str, Any]
) -> MetadataFile:
    return MetadataFile(
        path=relative_path,
        convention=convention,
        note=note,
        recognised_by=BY_CONTENT,
        filename_convention=None,
        kind=DOCUMENT,
        file=relative_path,
        basis=basis,
    )


def _flatten_context(context: Any) -> list[str]:
    """Every string reachable in an ``@context``, whatever shape it takes."""
    if isinstance(context, str):
        return [context.lower()]
    if isinstance(context, list):
        return [value for item in context for value in _flatten_context(item)]
    if isinstance(context, dict):
        return [value for item in context.values() for value in _flatten_context(item)]
    return []


def _is_frictionless_resources(resources: Any) -> bool:
    return (
        isinstance(resources, list)
        and len(resources) > 0
        and all(isinstance(item, dict) for item in resources)
        and any("path" in item or "data" in item for item in resources)
    )


def _is_datacite_record(document: dict[str, Any], by_lowered: dict[str, str]) -> bool:
    types = document.get(by_lowered.get("types", "types"))
    if isinstance(types, dict) and any(key.lower() == "resourcetypegeneral" for key in types):
        return True
    titles = document.get(by_lowered.get("titles", "titles"))
    creators = document.get(by_lowered.get("creators", "creators"))
    return bool(isinstance(titles, list) and titles and isinstance(creators, list) and creators)
