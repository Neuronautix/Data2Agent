"""Claim and evidence records.

The core invariant of Data2Agent:

    claim -> evidence -> source file -> immutable checksum

Every statement the system emits must be reducible to a named deterministic check
run over named bytes. A statement that cannot be reduced that way is not a claim
Data2Agent is allowed to make; there is no "probably" tier and no unsourced tier.
This is what later makes an unsupported-claim rate measurable rather than a
matter of opinion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# The registry of deterministic checks. A check id in an evidence record must
# appear here, so that "what was actually computed" is always answerable.
CHECKS: dict[str, str] = {
    "file.checksum": "SHA-256 over the complete file bytes",
    "file.size": "Size of the file in bytes",
    "file.format-detection": "Format determined from magic bytes and/or filename extension",
    "dataset.file-count": "Number of files inventoried under the dataset root",
    "dataset.identity": "SHA-256 fold over the sorted (path, file checksum) pairs",
    "convention.missing-values": (
        "The named missing-value convention applied when classifying cells, and its source"
    ),
    "table.row-count": "Number of data rows after the header row",
    "table.column-list": "Column names read from the header row, in order",
    "table.column-dtype": "Least upper bound of the token shapes observed in a column",
    "table.missing-value-count": (
        "Number of rows whose cell is empty, or holds a token the active "
        "missing-value convention resolves to missing"
    ),
    "table.missing-empty-count": "Number of rows whose cell in this column is empty",
    "table.missing-sentinel-count": (
        "Number of cells holding a token resolved to missing by the active convention"
    ),
    "table.ambiguous-token-count": (
        "Number of cells holding a token that was NOT resolved to missing by any convention"
    ),
    # One check for both kinds of table: the rule is the same code path for a
    # sheet and a delimited file, and the result names the sheet when there is one.
    "table.header-layout": (
        "Which row was taken as a table's header and where its data starts, by the "
        "named deterministic rule or by a layout declaration, with every row skipped "
        "before the data and the reason it was skipped (D2A-97)"
    ),
    "layout.declaration": (
        "A user-supplied layout declaration, identified by the SHA-256 of its bytes, "
        "that stated a table's header rows instead of the detection rule"
    ),
    # Workbook checks are distinct from their table.* counterparts on purpose.
    # A sheet-level claim carries a locator its delimited equivalent does not,
    # and collapsing the two would make a claim about one sheet indistinguishable
    # from a claim about the whole workbook (D2A-47).
    "workbook.row-count": (
        "Number of data rows below the header row of one worksheet, with the "
        "workbook path, sheet name and header row recorded"
    ),
    "workbook.missing-value-count": (
        "Number of rows whose cell is empty, or holds a token the active "
        "missing-value convention resolves to missing, for one column of one worksheet"
    ),
    "workbook.unprofiled": (
        "A workbook or worksheet was identified but its content could not be "
        "read; it carries no row count, because none was observed"
    ),
    "workbook.reader-unavailable": (
        "A file was identified as a workbook but not profiled, because the "
        "optional reader for that format is not installed"
    ),
    "json.shape": "Top-level type, keys and nesting depth of a JSON document",
    "metadata.file-convention": "Filename matches a published metadata convention",
    # Distinct from metadata.file-convention, and deliberately so: a claim that a
    # file's *name* follows a convention and a claim that its *structure* does
    # are different claims, checkable by different means, and collapsing them
    # would hide which of the two any given verdict rests on (D2A-49a).
    "metadata.content-signature": (
        "A file's structure matches a metadata record: a JSON document that names "
        "a published standard in its own content, or a table with a complete, unique "
        "subject-identifier column and grouping attribute columns"
    ),
    "metadata.candidate": (
        "A metadata recogniser applied to a file and could not finish reading it, so "
        "neither recognition nor absence of metadata is asserted for it"
    ),
    "identifier.detected": "A persistent-identifier pattern matched in file text",
}


@dataclass(frozen=True)
class EvidenceItem:
    """One deterministic observation, bound to the bytes it came from."""

    source: str  # dataset-relative path, or "" for dataset-level observations
    source_sha256: str  # "" only for dataset-level observations
    check: str
    result: Any
    field: str | None = None
    locator: str | None = None  # e.g. "column:sex", "line:12"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "source_sha256": self.source_sha256,
            "check": self.check,
            "result": self.result,
        }
        if self.field is not None:
            payload["field"] = self.field
        if self.locator is not None:
            payload["locator"] = self.locator
        return payload


@dataclass
class Claim:
    """A statement, plus the evidence that makes it sayable."""

    claim_id: str
    claim: str
    subject: str
    evidence: list[EvidenceItem] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim": self.claim,
            "subject": self.subject,
            "evidence": [item.as_dict() for item in self.evidence],
        }


def claim_id_for(claim: str, subject: str, evidence: list[EvidenceItem]) -> str:
    """Derive a stable claim id from the claim's content.

    Content-addressed rather than sequential, so the same dataset produces the
    same claim ids on every machine and the ledger can be diffed across runs.
    """
    payload = json.dumps(
        {"claim": claim, "subject": subject, "evidence": [item.as_dict() for item in evidence]},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "clm_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
