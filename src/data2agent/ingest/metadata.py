"""Metadata-file recognition.

A file is classified as metadata only when its *name* matches a published
convention (RO-Crate, ISA-Tab, BIDS, Datacite, Frictionless, a README). The
classification says "this file is conventionally metadata", nothing more: it does
not assert that the file is valid, complete, or even parseable. Schema validation
is a FAIR-profile concern and is deliberately absent from v0.1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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


@dataclass(frozen=True)
class MetadataFile:
    path: str
    convention: str
    note: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "convention": self.convention, "note": self.note}


def classify(relative_path: str) -> MetadataFile | None:
    """Return a classification when the filename matches a known convention."""
    name = relative_path.rsplit("/", 1)[-1].lower()
    for pattern, convention, note in _CONVENTIONS:
        if pattern.match(name):
            return MetadataFile(relative_path, convention, note)
    return None
