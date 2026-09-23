"""Profile rules, results and the context a check may read.

A check may look at the manifest, the evidence ledger, and the text of
recognised metadata files. It may **not** open the dataset itself. That
restriction is what keeps every profile verdict anchored to bytes that were
checksummed at ingest: a check that re-read the data could reach a conclusion
the evidence ledger cannot account for.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..evidence import EvidenceLedger

# The four outcomes, and only these four.
PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"
RESULTS = (PASS, FAIL, UNKNOWN, NOT_APPLICABLE)


@dataclass(frozen=True)
class Rule:
    """One canonical rule. The single source every projection is generated from."""

    id: str
    principle: str
    question: str
    check_type: str
    check_inputs: tuple[str, ...] = ()
    allowed_results: tuple[str, ...] = RESULTS
    inference_allowed: bool = False
    evidence_required: bool = True
    rationale_required_for: tuple[str, ...] = (FAIL, UNKNOWN)
    implemented: bool = True
    not_implemented_reason: str = ""
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "principle": self.principle,
            "question": self.question,
            "check": {"type": self.check_type, "inputs": list(self.check_inputs)},
            "allowed_results": list(self.allowed_results),
            "inference_allowed": self.inference_allowed,
            "evidence_required": self.evidence_required,
            "rationale_required_for": list(self.rationale_required_for),
            "implemented": self.implemented,
            **(
                {"not_implemented_reason": self.not_implemented_reason}
                if not self.implemented
                else {}
            ),
            **({"notes": self.notes} if self.notes else {}),
        }


@dataclass(frozen=True)
class Profile:
    """A named, versioned set of rules."""

    id: str
    version: str
    title: str
    description: str
    rules: tuple[Rule, ...]

    def rule(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        known = ", ".join(rule.id for rule in self.rules)
        raise KeyError(f"no rule '{rule_id}' in profile '{self.id}'; known rules: {known}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "rules": [rule.as_dict() for rule in self.rules],
        }


@dataclass
class CheckOutcome:
    """What a deterministic check concluded, and what it looked at."""

    result: str
    rationale: str = ""
    evidence: list[Any] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleResult:
    """A rule's outcome, in the shape ``assessment.schema.json`` requires."""

    rule_id: str
    principle: str
    result: str
    evidence: list[Any]
    rationale: str = ""
    inferred: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "rule_id": self.rule_id,
            "principle": self.principle,
            "result": self.result,
            "evidence": self.evidence,
            "inferred": self.inferred,
        }
        if self.rationale:
            payload["rationale"] = self.rationale
        return payload


@dataclass
class Assessment:
    dataset_id: str
    profile: Profile
    generator: dict[str, Any]
    results: list[RuleResult]
    unsupported_claims: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "assessment_version": "0.1.0",
            "dataset_id": self.dataset_id,
            "profile": {"id": self.profile.id, "version": self.profile.version},
            "generator": self.generator,
            "results": [result.as_dict() for result in self.results],
            # Empty for a deterministic run by construction: every result here
            # came from a check that cites what it read. A model-generated
            # assessment is scored by filling this list.
            "unsupported_claims": list(self.unsupported_claims),
        }

    def summary(self) -> dict[str, int]:
        counts = dict.fromkeys(RESULTS, 0)
        for result in self.results:
            counts[result.result] = counts.get(result.result, 0) + 1
        return counts


@dataclass
class ProfileContext:
    """Everything a check is allowed to read."""

    manifest: dict[str, Any]
    ledger: EvidenceLedger
    read_metadata: Callable[[str], str | None]
    _texts: dict[str, str] | None = field(default=None, repr=False)

    # -- convenience accessors, so checks stay short and uniform -------------

    @property
    def metadata_files(self) -> list[dict[str, Any]]:
        return self.manifest.get("metadata_files", [])

    @property
    def metadata_candidates(self) -> list[dict[str, Any]]:
        """Files a recogniser applied to and could not read. Open questions, not findings."""
        return self.manifest.get("metadata_candidates", [])

    @property
    def metadata_paths(self) -> set[str]:
        return {item["path"] for item in self.metadata_files}

    @property
    def files(self) -> list[dict[str, Any]]:
        return self.manifest.get("files", [])

    @property
    def data_files(self) -> list[dict[str, Any]]:
        """Files that are not a metadata document -- the payload of the dataset.

        Metadata recognised as ``embedded`` -- a registry worksheet inside a
        workbook of results -- does not remove its file from this list. The file
        is still payload, and keeping it here bounds the damage a wrong
        recognition can do: it can add a metadata finding, but it can never
        silently subtract a data file from the format and linkage checks.
        """
        documents = {
            item["path"]
            for item in self.metadata_files
            if item.get("kind", "document") == "document"
        }
        return [entry for entry in self.files if entry["path"] not in documents]

    @property
    def identifiers(self) -> list[dict[str, Any]]:
        return self.manifest.get("identifiers", [])

    @property
    def tables(self) -> dict[str, Any]:
        return self.manifest.get("tables", {})

    @property
    def missing_value_convention(self) -> dict[str, Any]:
        return self.manifest.get("missing_value_convention", {})

    def claim_ids(self, *, subject: str | None = None, check: str | None = None) -> list[str]:
        """Claim ids from the ingest ledger, cited rather than restated."""
        return [record.claim_id for record in self.ledger.query(subject=subject, check=check)]

    def metadata_text(self) -> dict[str, str]:
        """The text of every recognised metadata file that could be decoded.

        Metadata recognised inside a workbook, or in any file whose bytes are not
        text, yields nothing here. That is a gap the checks must report as one --
        see :meth:`unread_metadata` -- not treat as an absence of content.
        """
        if self._texts is None:
            texts: dict[str, str] = {}
            for item in self.metadata_files:
                content = self.read_metadata(item["path"])
                if content is not None:
                    texts[item["path"]] = content
            self._texts = texts
        return dict(self._texts)

    def unread_metadata(self) -> list[dict[str, Any]]:
        """Recognised metadata whose text this profile could not read.

        A check that searched only :meth:`metadata_text` must say so when this is
        non-empty; otherwise "no licence field was found" reads as a statement
        about a file nobody opened.
        """
        readable = set(self.metadata_text())
        return [item for item in self.metadata_files if item["path"] not in readable]
