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


def test_resolved_tokens_stay_attributable_to_a_named_convention(ingested):
    """NA reads as missing -- but never silently. The rule that did it is on the record."""
    strain = next(
        column
        for column in ingested.manifest["tables"]["animals.csv"]["columns"]
        if column["name"] == "strain"
    )
    assert strain["missing"] == 3
    assert strain["missing_empty"] == 0
    assert strain["missing_sentinel"] == 3
    assert strain["sentinel_tokens_seen"] == {"NA": 3}

    convention = ingested.manifest["missing_value_convention"]
    assert convention["id"] == "default-sentinels"
    assert "built-in default" in convention["source"]
    assert any("default-sentinels" in warning for warning in ingested.manifest["warnings"])


def test_ambiguous_tokens_are_never_resolved_by_a_built_in_convention(dataset_copy, tmp_path):
    """A cell reading 'unknown' may be a deliberate statement. We report, not decide."""
    from data2agent.ingest import ingest

    target = dataset_copy / "animals.csv"
    target.write_text(target.read_text().replace(",F,", ",unknown,", 1), encoding="utf-8")
    result = ingest(dataset_copy, tmp_path / "out")

    sex = next(
        column
        for column in result.manifest["tables"]["animals.csv"]["columns"]
        if column["name"] == "sex"
    )
    assert sex["ambiguous_tokens_seen"] == {"unknown": 1}
    assert sex["missing"] == 12, "the ambiguous token must not join the missing count"
    assert any("NOT resolved to missing" in warning for warning in result.manifest["warnings"])
