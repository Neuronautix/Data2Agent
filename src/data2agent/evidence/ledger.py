"""The evidence ledger: an append-only record of every claim made about a dataset."""

from __future__ import annotations

from typing import Any

from .. import EVIDENCE_VERSION
from .model import CHECKS, Claim, EvidenceItem, claim_id_for


class EvidenceLedger:
    """Collects claims and serialises them to ``evidence.json``.

    The ledger refuses unknown check ids. That refusal is the enforcement point
    for the whole invariant: a fact can only enter the record through a check
    someone has named and implemented.
    """

    def __init__(self, dataset_id: str) -> None:
        self.dataset_id = dataset_id
        self._claims: list[Claim] = []
        self._index: dict[str, Claim] = {}

    def record(self, claim: str, subject: str, evidence: list[EvidenceItem]) -> Claim:
        if not evidence:
            raise ValueError(f"refusing to record a claim with no evidence: {claim!r}")
        for item in evidence:
            if item.check not in CHECKS:
                raise ValueError(
                    f"unknown check id {item.check!r}; add it to evidence.model.CHECKS"
                )
        identifier = claim_id_for(claim, subject, evidence)
        existing = self._index.get(identifier)
        if existing is not None:
            return existing
        record = Claim(claim_id=identifier, claim=claim, subject=subject, evidence=list(evidence))
        self._claims.append(record)
        self._index[identifier] = record
        return record

    def get(self, claim_id: str) -> Claim | None:
        return self._index.get(claim_id)

    def query(
        self,
        *,
        subject: str | None = None,
        check: str | None = None,
        contains: str | None = None,
    ) -> list[Claim]:
        needle = contains.lower() if contains else None
        results = []
        for record in self._claims:
            if subject is not None and record.subject != subject:
                continue
            if check is not None and all(item.check != check for item in record.evidence):
                continue
            if needle is not None and needle not in record.claim.lower():
                continue
            results.append(record)
        return results

    def __len__(self) -> int:
        return len(self._claims)

    @property
    def claims(self) -> list[Claim]:
        return list(self._claims)

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_version": EVIDENCE_VERSION,
            "dataset_id": self.dataset_id,
            "checks": dict(sorted(CHECKS.items())),
            "claims": [record.as_dict() for record in sorted(self._claims, key=_sort_key)],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EvidenceLedger:
        ledger = cls(payload.get("dataset_id", ""))
        for entry in payload.get("claims", []):
            record = Claim(
                claim_id=entry["claim_id"],
                claim=entry["claim"],
                subject=entry.get("subject", ""),
                evidence=[
                    EvidenceItem(**_evidence_kwargs(item)) for item in entry.get("evidence", [])
                ],
            )
            ledger._claims.append(record)
            ledger._index[record.claim_id] = record
        return ledger


def _evidence_kwargs(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": item.get("source", ""),
        "source_sha256": item.get("source_sha256", ""),
        "check": item["check"],
        "result": item.get("result"),
        "field": item.get("field"),
        "locator": item.get("locator"),
    }


def _sort_key(record: Claim) -> tuple[str, str]:
    return (record.subject, record.claim_id)
