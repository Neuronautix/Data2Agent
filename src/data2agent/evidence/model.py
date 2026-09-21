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
    "table.row-count": "Number of data rows after the header row",
    "table.column-list": "Column names read from the header row, in order",
    "table.column-dtype": "Least upper bound of the token shapes observed in a column",
    "table.missing-value-count": "Number of rows whose cell in this column is empty",
    "table.null-like-token-count": "Number of cells holding a conventional null-like token",
    "json.shape": "Top-level type, keys and nesting depth of a JSON document",
    "metadata.file-convention": "Filename matches a published metadata convention",
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
