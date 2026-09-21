"""Deterministic implementations of the FAIR profile's checks.

Each check reads the manifest, the evidence ledger and the text of recognised
metadata files, and returns one of four outcomes with the evidence it consulted.
None of them reads the dataset directly, and none of them guesses: where the
local snapshot cannot settle a question, the answer is ``unknown`` with a
rationale saying why.

Evidence is cited as claim ids from ``evidence.json`` wherever a claim already
exists, and as inline records for facts the check itself computed. Restating a
claim's content would duplicate it; citing it keeps one source of truth.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..model import (
    FAIL,
    NOT_APPLICABLE,
    PASS,
    UNKNOWN,
    CheckOutcome,
    ProfileContext,
    Rule,
)

# Identifier schemes that can identify a *dataset*. ORCID identifies a person
# and a bare URI is not persistent, so neither satisfies F1 on its own.
_PERSISTENT_SCHEMES = {"doi", "ror"}
_PERSISTENT_URI_MARKERS = (
    "doi.org/",
    "hdl.handle.net/",
    "n2t.net/ark:",
    "/ark:/",
    "w3id.org/",
    "purl.org/",
)

# Formats we are prepared to call open and documented, and formats we are not.
# An explicit list beats an opinion; anything in neither list is unknown.
_OPEN_FORMATS = frozenset(
    {
        "csv",
        "tsv",
        "json",
        "jsonld",
        "xml",
        "yaml",
        "turtle",
        "ntriples",
        "rdfxml",
        "markdown",
        "text",
        "png",
        "jpeg",
        "tiff",
        "pdf",
        "zip",
        "gzip",
        "parquet",
        "hdf5",
        "nwb",
        "nifti",
        "edf",
        "sqlite",
    }
)
_CLOSED_FORMATS = frozenset({"matlab", "xlsx"})

# Namespaces whose appearance in metadata evidences a controlled vocabulary.
_VOCABULARY_MARKERS = (
    "schema.org",
    "purl.obolibrary.org",
    "purl.org/dc/",
    "www.w3.org/ns/",
    "xmlns.com/foaf/",
    "identifiers.org/",
    "bioportal.bioontology.org",
    "edamontology.org",
    "obofoundry.org",
    "vocab.nerc.ac.uk",
    "@context",
)

_LICENCE_KEYS = ("license", "licence", "rights", "licenseurl", "dct:license", "spdx")
_PROVENANCE_KEYS = (
    "author",
    "authors",
    "creator",
    "creators",
    "contributor",
    "contributors",
    "publisher",
    "producedby",
    "wasderivedfrom",
    "provenance",
    "source",
    "citation",
)

# Filename conventions that are community metadata standards. A README is
# metadata; it is not a standard.
_COMMUNITY_STANDARDS = {"ro-crate", "frictionless", "bids", "isa-tab", "codemeta", "datacite"}


def identifier_presence(rule: Rule, context: ProfileContext) -> CheckOutcome:
    metadata_paths = context.metadata_paths
    if not metadata_paths:
        return CheckOutcome(
            result=FAIL,
            rationale=(
                "no file was recognised as dataset metadata, so no metadata can carry an identifier"
            ),
            evidence=[_inline("metadata.file-convention", [], "no recognised metadata files")],
        )

    hits = [
        hit
        for hit in context.identifiers
        if hit["source"] in metadata_paths and _is_persistent(hit)
    ]
    if hits:
        return CheckOutcome(
            result=PASS,
            rationale=(
                f"{len(hits)} persistent-identifier-shaped value(s) found in dataset metadata; "
                f"whether they resolve is F1-PID-RESOLVABLE and was not checked"
            ),
            evidence=_identifier_evidence(context, hits),
            observations={"identifiers": [hit["value"] for hit in hits]},
        )

    other = [hit["value"] for hit in context.identifiers if hit["source"] in metadata_paths]
    return CheckOutcome(
        result=FAIL,
        rationale=(
            "no DOI-, handle-, ARK- or ROR-shaped identifier appears in any recognised "
            "metadata file" + (f"; the identifiers that do appear are {other}" if other else "")
        ),
        evidence=[
            _inline(
                "identifier.detected",
                other,
                f"scanned {len(metadata_paths)} metadata file(s) for persistent identifiers",
            )
        ],
    )


def metadata_presence(rule: Rule, context: ProfileContext) -> CheckOutcome:
    if not context.files:
        return CheckOutcome(
            result=NOT_APPLICABLE,
            rationale="the dataset contains no files, so there is nothing for metadata to describe",
            evidence=[_inline("dataset.file-count", 0, "empty dataset")],
        )

    recognised = context.metadata_files
    if recognised:
        return CheckOutcome(
            result=PASS,
            rationale="",
            evidence=context.claim_ids(check="metadata.file-convention")
            or [_inline("metadata.file-convention", [item["path"] for item in recognised], "")],
            observations={"metadata_files": [item["path"] for item in recognised]},
        )
    return CheckOutcome(
        result=FAIL,
        rationale=(
            f"none of the {len(context.files)} file(s) matched a recognised metadata "
            f"filename convention; note that recognition is by filename only, so metadata "
            f"under an unconventional name would not be counted"
        ),
        evidence=[
            _inline("metadata.file-convention", [], "no filename matched a known convention")
        ],
    )


def metadata_references_data(rule: Rule, context: ProfileContext) -> CheckOutcome:
    data_files = context.data_files
    if not data_files:
        return CheckOutcome(
            result=NOT_APPLICABLE,
            rationale="the dataset contains no non-metadata files to reference",
            evidence=[_inline("dataset.file-count", len(context.files), "metadata-only dataset")],
        )

    texts = context.metadata_text()
    if not texts:
        return CheckOutcome(
            result=UNKNOWN,
            rationale=(
                "no recognised metadata file could be read as text, so whether the data "
                "files are referenced could not be established"
            ),
            evidence=[
                _inline("metadata.file-convention", sorted(context.metadata_paths), "unreadable")
            ],
        )

    combined = "\n".join(texts.values())
    unreferenced = [
        entry["path"]
        for entry in data_files
        if entry["path"] not in combined and entry["path"].rsplit("/", 1)[-1] not in combined
    ]
    if not unreferenced:
        return CheckOutcome(
            result=PASS,
            rationale="",
            evidence=[
                _inline(
                    "metadata.file-convention",
                    sorted(texts),
                    f"all {len(data_files)} data file name(s) appear in metadata text",
                )
            ],
        )
    return CheckOutcome(
        result=FAIL,
        rationale=(
            f"{len(unreferenced)} of {len(data_files)} data file(s) are not named in any "
            f"metadata file: {unreferenced}"
        ),
        evidence=[
            _inline("metadata.file-convention", sorted(texts), f"unreferenced: {unreferenced}")
        ],
        observations={"unreferenced": unreferenced},
    )


def metadata_machine_readable(rule: Rule, context: ProfileContext) -> CheckOutcome:
    recognised = context.metadata_files
    if not recognised:
        return CheckOutcome(
            result=NOT_APPLICABLE,
            rationale="no recognised metadata file, so there is nothing whose format to assess",
            evidence=[_inline("metadata.file-convention", [], "no recognised metadata files")],
        )

    by_path = {entry["path"]: entry for entry in context.files}
    structured = [
        item["path"]
        for item in recognised
        if by_path.get(item["path"], {}).get("format")
        in {"json", "jsonld", "xml", "yaml", "turtle", "ntriples", "rdfxml"}
    ]
    if structured:
        return CheckOutcome(
            result=PASS,
            rationale="",
            evidence=context.claim_ids(check="json.shape")
            or [_inline("file.format-detection", structured, "structured metadata present")],
            observations={"structured_metadata": structured},
        )
    return CheckOutcome(
        result=FAIL,
        rationale=(
            f"metadata exists ({[item['path'] for item in recognised]}) but only in prose "
            f"formats; nothing is indexable without parsing free text"
        ),
        evidence=[
            _inline(
                "file.format-detection",
                [by_path.get(item["path"], {}).get("format") for item in recognised],
                "no structured metadata format found",
            )
        ],
    )


def format_openness(rule: Rule, context: ProfileContext) -> CheckOutcome:
    data_files = context.data_files
    if not data_files:
        return CheckOutcome(
            result=NOT_APPLICABLE,
            rationale="the dataset contains no non-metadata files whose format to assess",
            evidence=[_inline("dataset.file-count", len(context.files), "metadata-only dataset")],
        )

    closed = sorted({entry["format"] for entry in data_files if entry["format"] in _CLOSED_FORMATS})
    unclassified = sorted(
        {
            entry["format"]
            for entry in data_files
            if entry["format"] not in _OPEN_FORMATS and entry["format"] not in _CLOSED_FORMATS
        }
    )
    evidence = context.claim_ids(check="file.format-detection") or [
        _inline("file.format-detection", sorted({entry["format"] for entry in data_files}), "")
    ]

    if closed:
        return CheckOutcome(
            result=FAIL,
            rationale=f"data files use format(s) not on the open-format list: {closed}",
            evidence=evidence,
            observations={"closed_formats": closed},
        )
    if unclassified:
        # One unidentified file is enough to make "all formats are open"
        # unsupportable, so the whole rule goes unknown rather than mostly-pass.
        return CheckOutcome(
            result=UNKNOWN,
            rationale=(
                f"format(s) {unclassified} are neither on the open list nor the closed list, "
                f"so whether every data file is in an open format cannot be established"
            ),
            evidence=evidence,
            observations={"unclassified_formats": unclassified},
        )
    return CheckOutcome(result=PASS, rationale="", evidence=evidence)


def vocabulary_reference(rule: Rule, context: ProfileContext) -> CheckOutcome:
    texts = context.metadata_text()
    if not texts:
        return CheckOutcome(
            result=UNKNOWN,
            rationale="no recognised metadata could be read, so vocabulary use cannot be assessed",
            evidence=[
                _inline("metadata.file-convention", sorted(context.metadata_paths), "unreadable")
            ],
        )

    found: dict[str, list[str]] = {}
    for path, text in texts.items():
        lowered = text.lower()
        markers = [marker for marker in _VOCABULARY_MARKERS if marker in lowered]
        if markers:
            found[path] = markers

    if found:
        return CheckOutcome(
            result=PASS,
            rationale="",
            evidence=[
                _inline("metadata.file-convention", found, "vocabulary namespaces referenced")
            ],
            observations={"references": found},
        )
    return CheckOutcome(
        result=FAIL,
        rationale=(
            f"no reference to a known controlled vocabulary or ontology namespace was found "
            f"in {sorted(texts)}; terms appear as free text with no shared definition"
        ),
        evidence=[
            _inline("metadata.file-convention", sorted(texts), "no vocabulary namespace found")
        ],
    )


def license_declared(rule: Rule, context: ProfileContext) -> CheckOutcome:
    licence_files = [
        item["path"] for item in context.metadata_files if item["convention"] == "license"
    ]
    if licence_files:
        return CheckOutcome(
            result=PASS,
            rationale="",
            evidence=[_inline("metadata.file-convention", licence_files, "dedicated licence file")],
        )

    declarations = _find_keys(context, _LICENCE_KEYS)
    if declarations:
        return CheckOutcome(
            result=PASS,
            rationale="",
            evidence=[_inline("json.shape", declarations, "licence field declared in metadata")],
            observations={"declarations": declarations},
        )

    if not context.metadata_files:
        return CheckOutcome(
            result=FAIL,
            rationale="no licence file and no metadata in which a licence could be declared",
            evidence=[_inline("metadata.file-convention", [], "no recognised metadata files")],
        )
    return CheckOutcome(
        result=FAIL,
        rationale=(
            f"no LICENSE file, and no licence field in {sorted(context.metadata_paths)}; "
            f"reuse conditions are therefore unstated"
        ),
        evidence=[
            _inline("metadata.file-convention", sorted(context.metadata_paths), "no licence found")
        ],
    )


def provenance_declared(rule: Rule, context: ProfileContext) -> CheckOutcome:
    if not context.metadata_files:
        return CheckOutcome(
            result=FAIL,
            rationale="no recognised metadata in which provenance could be stated",
            evidence=[_inline("metadata.file-convention", [], "no recognised metadata files")],
        )

    declarations = _find_keys(context, _PROVENANCE_KEYS)
    if declarations:
        return CheckOutcome(
            result=PASS,
            rationale="",
            evidence=[
                _inline("json.shape", declarations, "provenance fields declared in metadata")
            ],
            observations={"declarations": declarations},
        )
    return CheckOutcome(
        result=FAIL,
        rationale=(
            f"no author, creator, publisher or source field found in "
            f"{sorted(context.metadata_paths)}; the data's origin is unstated"
        ),
        evidence=[
            _inline(
                "metadata.file-convention", sorted(context.metadata_paths), "no provenance fields"
            )
        ],
    )


def community_standard(rule: Rule, context: ProfileContext) -> CheckOutcome:
    recognised = context.metadata_files
    if not recognised:
        return CheckOutcome(
            result=NOT_APPLICABLE,
            rationale="no recognised metadata file, so no community standard can be claimed",
            evidence=[_inline("metadata.file-convention", [], "no recognised metadata files")],
        )

    standards = sorted(
        {item["convention"] for item in recognised if item["convention"] in _COMMUNITY_STANDARDS}
    )
    if standards:
        return CheckOutcome(
            result=PASS,
            rationale="",
            evidence=context.claim_ids(check="metadata.file-convention")
            or [_inline("metadata.file-convention", standards, "")],
            observations={"standards": standards},
        )
    conventions = sorted({item["convention"] for item in recognised})
    return CheckOutcome(
        result=FAIL,
        rationale=(
            f"metadata follows no recognised community standard; what was found is "
            f"{conventions}, which is documentation rather than a shared schema"
        ),
        evidence=[
            _inline("metadata.file-convention", conventions, "no community standard matched")
        ],
    )


def missing_value_convention_declared(rule: Rule, context: ProfileContext) -> CheckOutcome:
    tables = context.tables
    if not tables:
        return CheckOutcome(
            result=NOT_APPLICABLE,
            rationale=(
                "the dataset contains no profiled tables, so no cell-level convention applies"
            ),
            evidence=[_inline("dataset.file-count", len(context.files), "no tabular data")],
        )

    sentinels: dict[str, dict[str, Any]] = {}
    ambiguous: dict[str, dict[str, Any]] = {}
    for path, profile in tables.items():
        for column in profile.get("columns", []):
            if column.get("sentinel_tokens_seen"):
                sentinels.setdefault(path, {})[column["name"]] = column["sentinel_tokens_seen"]
            if column.get("ambiguous_tokens_seen"):
                ambiguous.setdefault(path, {})[column["name"]] = column["ambiguous_tokens_seen"]

    convention = context.missing_value_convention
    evidence = context.claim_ids(check="convention.missing-values") or [
        _inline("convention.missing-values", convention, "")
    ]

    if not sentinels and not ambiguous:
        return CheckOutcome(
            result=NOT_APPLICABLE,
            rationale=(
                "no cell in any table holds a sentinel or ambiguous token, so there is no "
                "convention the dataset needs to declare"
            ),
            evidence=evidence,
        )

    if convention.get("id") == "declared":
        return CheckOutcome(
            result=PASS,
            rationale="",
            evidence=evidence,
            observations={"source": convention.get("source"), "sentinels": sentinels},
        )

    observed = sorted(
        {token for columns in sentinels.values() for tokens in columns.values() for token in tokens}
    )
    observed_ambiguous = sorted(
        {token for columns in ambiguous.values() for tokens in columns.values() for token in tokens}
    )
    return CheckOutcome(
        result=FAIL,
        rationale=(
            f"tables hold absence-like token(s) {observed + observed_ambiguous} but the "
            f"dataset declares no missing-value convention; Data2Agent resolved them under "
            f"'{convention.get('id')}' ({convention.get('source')}), and a different tool "
            f"will resolve them differently"
        ),
        evidence=evidence,
        observations={"sentinel_tokens": sentinels, "ambiguous_tokens": ambiguous},
    )


CHECKS = {
    "identifier_presence": identifier_presence,
    "metadata_presence": metadata_presence,
    "metadata_references_data": metadata_references_data,
    "metadata_machine_readable": metadata_machine_readable,
    "format_openness": format_openness,
    "vocabulary_reference": vocabulary_reference,
    "license_declared": license_declared,
    "provenance_declared": provenance_declared,
    "community_standard": community_standard,
    "missing_value_convention_declared": missing_value_convention_declared,
}


# -- helpers ---------------------------------------------------------------


def _inline(check: str, result: Any, detail: str) -> dict[str, Any]:
    """An evidence record for a fact this check computed itself."""
    payload: dict[str, Any] = {"check": check, "result": result}
    if detail:
        payload["detail"] = detail
    return payload


def _is_persistent(hit: dict[str, Any]) -> bool:
    if hit["scheme"] in _PERSISTENT_SCHEMES:
        return True
    value = hit["value"].lower()
    return hit["scheme"] == "uri" and any(marker in value for marker in _PERSISTENT_URI_MARKERS)


def _identifier_evidence(context: ProfileContext, hits: list[dict[str, Any]]) -> list[Any]:
    """Prefer citing the ingest ledger's claims over restating their content."""
    values = {hit["value"] for hit in hits}
    cited = [
        record.claim_id
        for record in context.ledger.query(check="identifier.detected")
        if any(item.result in values for item in record.evidence)
    ]
    return cited or [_inline("identifier.detected", sorted(values), "")]


def _find_keys(context: ProfileContext, keys: tuple[str, ...]) -> dict[str, list[str]]:
    """Find which of ``keys`` appear as keys in structured metadata documents.

    Keys are matched case-insensitively and ignoring any namespace prefix, so
    ``dct:license`` and ``License`` both count. Prose metadata is searched only
    for an explicit ``key:`` line, never for the bare word -- a README that
    happens to contain "author" is not a provenance declaration.
    """
    found: dict[str, list[str]] = {}
    for path, text in context.metadata_text().items():
        matched: set[str] = set()
        try:
            document = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            document = None

        if document is not None:
            for key in _walk_keys(document):
                bare = key.split(":")[-1].lower().replace("_", "").replace(" ", "")
                if bare in keys:
                    matched.add(key)
        else:
            for key in keys:
                if re.search(
                    rf"^\s*[\"']?{re.escape(key)}[\"']?\s*:", text, re.IGNORECASE | re.MULTILINE
                ):
                    matched.add(key)
        if matched:
            found[path] = sorted(matched)
    return found


def _walk_keys(node: Any) -> list[str]:
    if isinstance(node, dict):
        keys = list(node.keys())
        for value in node.values():
            keys.extend(_walk_keys(value))
        return keys
    if isinstance(node, list):
        keys: list[str] = []
        for item in node:
            keys.extend(_walk_keys(item))
        return keys
    return []
