"""The six v0.1 tools, plus the integrity guard that protects their answers."""

from __future__ import annotations

from pathlib import Path

import pytest

from data2agent.errors import OutputError
from data2agent.ingest import ingest
from data2agent.mcp import DatasetService


@pytest.fixture
def service(ingested) -> DatasetService:
    return DatasetService(ingested.output_dir)


def test_dataset_inventory_reports_identity_and_warnings(service, ingested):
    summary = service.dataset_inventory()
    assert summary["dataset_id"] == ingested.dataset_id
    assert summary["file_count"] == 4
    assert summary["table_count"] == 2
    assert summary["mode"] == "structured"
    # "not determined" must be distinguishable from "none".
    assert summary["relationships_determined"] is False


def test_list_files_filters_by_glob_and_format(service):
    assert {entry["path"] for entry in service.list_files(pattern="*.csv")} == {
        "animals.csv",
        "observations.csv",
    }
    assert [entry["path"] for entry in service.list_files(file_format="json")] == [
        "dataset_description.json"
    ]


def test_inspect_file_returns_checksum_and_bounded_preview(service):
    payload = service.inspect_file("animals.csv", preview_bytes=64)
    assert payload["sha256"] and payload["integrity"]["matches"] is True
    assert payload["preview_bytes"] == 64
    assert payload["preview_truncated"] is True
    assert payload["preview"].startswith("animal_id,strain")


def test_inspect_table_returns_the_recorded_profile(service):
    profile = service.inspect_table("animals.csv")
    assert profile["rows"] == 48
    assert profile["missing"]["sex"] == 12
    assert profile["integrity"]["matches"] is True


def test_inspect_table_refuses_a_non_table(service):
    with pytest.raises(KeyError, match="not profiled as a table"):
        service.inspect_table("README.md")


def test_get_metadata_lists_then_serves_verbatim(service, example_dataset: Path):
    listing = service.get_metadata()
    assert {item["path"] for item in listing["metadata_files"]} == {
        "README.md",
        "dataset_description.json",
    }
    served = service.get_metadata("dataset_description.json")
    assert served["content"] == (example_dataset / "dataset_description.json").read_text(
        encoding="utf-8"
    )


def test_get_metadata_refuses_a_non_metadata_file(service):
    with pytest.raises(KeyError, match="not a recognised metadata file"):
        service.get_metadata("animals.csv")


def test_get_evidence_queries_by_subject_check_and_id(service):
    by_check = service.get_evidence(subject="animals.csv", check="table.missing-value-count")
    assert by_check["total"] >= 1
    claim_id = by_check["claims"][0]["claim_id"]
    assert service.get_evidence(claim_id=claim_id)["claims"][0]["claim_id"] == claim_id
    with pytest.raises(KeyError):
        service.get_evidence(claim_id="clm_ffffffffffffffff")


def test_resolve_identifier_is_lookup_not_resolution(service):
    found = service.resolve_identifier("10.5281/zenodo.0000000")
    assert found["found"] is True
    assert found["resolved"] is None, "v0.1 must never claim an identifier resolves"
    assert service.resolve_identifier("10.9999/nope")["found"] is False


def test_content_is_withheld_when_the_source_drifts(dataset_copy: Path, tmp_path: Path):
    """An answer drawn from changed bytes looks exactly like a good answer."""
    result = ingest(dataset_copy, tmp_path / "out")
    service = DatasetService(result.output_dir)
    assert service.inspect_file("animals.csv")["integrity"]["matches"] is True

    (dataset_copy / "animals.csv").write_text("animal_id\nA001\n", encoding="utf-8")
    payload = service.inspect_file("animals.csv")
    assert payload["integrity"]["matches"] is False
    assert payload["preview"] is None
    assert "re-ingest" in payload["preview_withheld"]

    verification = service.verify_dataset()
    assert verification["intact"] is False
    assert verification["mismatched"][0]["path"] == "animals.csv"


def test_paths_cannot_escape_the_dataset_root(service):
    with pytest.raises(KeyError):
        service.inspect_file("../../etc/passwd")


def test_resources_are_served_as_json_text(service):
    for uri in (
        "dataset://manifest",
        "dataset://provenance",
        "dataset://evidence",
        "dataset://metadata",
        "dataset://files/animals.csv",
    ):
        assert service.resource(uri).strip().startswith("{")
    with pytest.raises(KeyError):
        service.resource("dataset://nope")


def test_service_needs_an_ingested_directory(tmp_path: Path):
    with pytest.raises(OutputError, match="run `data2agent ingest` first"):
        DatasetService(tmp_path)
