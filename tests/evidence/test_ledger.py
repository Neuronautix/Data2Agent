"""Acceptance: every reported fact references evidence, and nothing is invented."""

from __future__ import annotations

import pytest

from data2agent.evidence import CHECKS, EvidenceItem, EvidenceLedger


def test_a_claim_without_evidence_is_refused():
    ledger = EvidenceLedger("sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="no evidence"):
        ledger.record("the animals are healthy", subject="dataset", evidence=[])


def test_an_unregistered_check_is_refused():
    ledger = EvidenceLedger("sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="unknown check id"):
        ledger.record(
            "sex is female for the unrecorded animals",
            subject="animals.csv",
            evidence=[EvidenceItem("animals.csv", "a" * 64, "vibes.inference", "F")],
        )


def test_claim_ids_are_content_addressed_and_stable():
    def build():
        ledger = EvidenceLedger("sha256:" + "0" * 64)
        return ledger.record(
            "'a.csv' has 3 data row(s)",
            subject="a.csv",
            evidence=[EvidenceItem("a.csv", "a" * 64, "table.row-count", 3)],
        ).claim_id

    assert build() == build()
    assert build().startswith("clm_")


def test_every_manifest_table_fact_has_a_supporting_claim(ingested):
    """Walk the manifest and demand a claim for each reported number."""
    ledger = ingested.evidence
    for path, profile in ingested.manifest["tables"].items():
        row_claims = ledger.query(subject=path, check="table.row-count")
        assert any(item.result == profile["rows"] for c in row_claims for item in c.evidence)
        for column in profile["columns"]:
            dtype_claims = [
                item
                for claim in ledger.query(subject=path, check="table.column-dtype")
                for item in claim.evidence
                if item.field == column["name"]
            ]
            assert dtype_claims, f"no dtype claim for {path}:{column['name']}"
            assert dtype_claims[0].result == column["dtype"]
            if column["missing"]:
                missing_claims = [
                    item
                    for claim in ledger.query(subject=path, check="table.missing-value-count")
                    for item in claim.evidence
                    if item.field == column["name"]
                ]
                assert missing_claims and missing_claims[0].result == column["missing"]


def test_every_evidence_item_names_a_registered_check(ingested):
    for claim in ingested.evidence.claims:
        for item in claim.evidence:
            assert item.check in CHECKS


def test_every_missingness_claim_cites_the_convention_that_produced_it(ingested):
    """The number is only meaningful alongside the rule that generated it."""
    claims = ingested.evidence.query(check="table.missing-value-count")
    assert claims
    for claim in claims:
        checks = {item.check for item in claim.evidence}
        assert "convention.missing-values" in checks, (
            f"missingness claim {claim.claim_id} does not cite the convention it used"
        )
        convention = next(
            item for item in claim.evidence if item.check == "convention.missing-values"
        )
        assert convention.result["id"] == "default-sentinels"
        assert convention.result["ambiguous_tokens_resolved"] is False


def test_missing_total_equals_its_two_components(ingested):
    for claim in ingested.evidence.query(check="table.missing-value-count"):
        by_check = {item.check: item.result for item in claim.evidence}
        assert by_check["table.missing-value-count"] == (
            by_check["table.missing-empty-count"] + by_check["table.missing-sentinel-count"]
        )


def test_file_level_evidence_carries_the_files_checksum(ingested):
    checksums = {entry["path"]: entry["sha256"] for entry in ingested.manifest["files"]}
    for claim in ingested.evidence.claims:
        for item in claim.evidence:
            if item.source:
                assert item.source_sha256 == checksums[item.source]


def test_no_claim_asserts_a_value_for_a_missing_cell(ingested):
    """The 12 animals without a recorded sex must never acquire one."""
    sex_claims = [
        claim
        for claim in ingested.evidence.query(subject="animals.csv")
        if "sex" in claim.claim.lower()
    ]
    assert sex_claims
    for claim in sex_claims:
        for item in claim.evidence:
            assert item.check in {
                "table.column-dtype",
                "table.missing-value-count",
                "table.missing-empty-count",
                "table.missing-sentinel-count",
                "table.ambiguous-token-count",
                "table.column-list",
                "convention.missing-values",
            }
    missing = [claim for claim in sex_claims if "is missing for" in claim.claim]
    assert len(missing) == 1
    assert missing[0].evidence[0].result == 12


def test_ledger_round_trips_through_json(ingested):
    restored = EvidenceLedger.from_dict(ingested.evidence.as_dict())
    assert len(restored) == len(ingested.evidence)
    assert restored.as_dict()["claims"] == ingested.evidence.as_dict()["claims"]
