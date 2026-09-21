"""Acceptance: no invented metadata.

These tests encode the rule negatively -- they assert that plausible-but-unstated
facts are absent from the output. They are the ones most likely to fail when
someone later adds a well-meaning "helpful" inference.
"""

from __future__ import annotations

import json

# Terms that a language model would plausibly supply for a preclinical dataset,
# and that appear nowhere in the example dataset's bytes.
UNSTATED_TERMS = [
    "male mice",
    "female mice",
    "wild type control group",
    "morris water maze",
    "acquisition device",
    "isoflurane",
    "randomised",
    "approved by",
    "ethics",
    "IACUC",
]


def test_manifest_contains_no_unstated_domain_terms(ingested):
    serialised = json.dumps(ingested.manifest).lower()
    for term in UNSTATED_TERMS:
        assert term.lower() not in serialised, f"manifest invented '{term}'"


def test_evidence_contains_no_unstated_domain_terms(ingested):
    serialised = json.dumps(ingested.evidence.as_dict()).lower()
    for term in UNSTATED_TERMS:
        assert term.lower() not in serialised, f"evidence ledger invented '{term}'"


def test_relationships_are_reported_as_undetermined_not_as_absent(ingested):
    """An empty list here means 'not determined'; the API must say which."""
    assert ingested.manifest["relationships"] == []


def test_identifiers_are_never_reported_as_resolved(ingested):
    for hit in ingested.manifest["identifiers"]:
        assert "resolved" not in hit
        assert "valid" not in hit


def test_null_like_tokens_are_not_folded_into_missingness(ingested):
    strain = next(
        column
        for column in ingested.manifest["tables"]["animals.csv"]["columns"]
        if column["name"] == "strain"
    )
    assert strain["null_like_tokens"] == 3
    assert strain["missing"] == 0
    assert any("null-like" in warning for warning in ingested.manifest["warnings"])
