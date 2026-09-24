"""The six v0.1 tools, plus the integrity guard that protects their answers."""

from __future__ import annotations

import json
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


def test_list_tables_and_read_rows_expose_observations(service):
    listing = service.list_tables()
    assert listing["total"] == 2
    assert {table["path"] for table in listing["tables"]} == {
        "animals.csv",
        "observations.csv",
    }

    payload = service.read_rows(
        "animals.csv",
        columns=["animal_id", "weight_g"],
        offset=0,
        limit=2,
    )
    assert payload["returned"] == 2
    assert payload["rows"][0]["source_row"] == 2
    assert set(payload["rows"][0]["values"]) == {"animal_id", "weight_g"}
    assert isinstance(payload["rows"][0]["values"]["weight_g"], int)
    assert payload["integrity"]["matches"] is True
    assert payload["limit_applied"] == 2


def test_read_rows_is_bounded(service):
    payload = service.read_rows("animals.csv", limit=100_000)
    assert payload["limit_requested"] == 100_000
    assert payload["limit_applied"] == 1000
    assert payload["returned"] == 48


def test_filter_rows_selects_a_group_and_reports_total_matches(service):
    payload = service.filter_rows(
        "animals.csv",
        filters=[{"column": "genotype", "op": "eq", "value": "KO"}],
        columns=["animal_id", "genotype", "weight_g"],
        limit=3,
    )

    assert payload["returned"] == 3
    assert payload["matches_in_scanned_rows"] == 24
    assert payload["truncated"] is True
    assert all(row["values"]["genotype"] == "KO" for row in payload["rows"])
    assert payload["operation"]["filters"][0]["column"] == "genotype"
    assert payload["input"]["backing_sha256"]


def test_aggregate_computes_group_counts_and_numeric_means(service):
    payload = service.aggregate(
        "animals.csv",
        group_by=["genotype"],
        metrics=[
            {"op": "count", "name": "n"},
            {"op": "mean", "column": "weight_g", "name": "mean_weight_g"},
        ],
    )

    groups = {item["group"]["genotype"]: item["metrics"] for item in payload["groups"]}
    assert groups["KO"]["n"] == 24
    assert groups["WT"]["n"] == 24
    assert groups["KO"]["mean_weight_g"] == pytest.approx(24.0)
    assert groups["WT"]["mean_weight_g"] == pytest.approx(24.041666666666668)
    assert payload["rows_included"] == 48
    assert payload["input"]["scan_complete"] is True


def test_aggregate_can_filter_before_grouping(service):
    payload = service.aggregate(
        "observations.csv",
        group_by=["session"],
        filters=[{"column": "animal_id", "op": "in", "value": ["A001", "A002"]}],
        metrics=[
            {"op": "count", "name": "n"},
            {"op": "mean", "column": "latency_s", "name": "mean_latency"},
        ],
    )

    assert [item["group"]["session"] for item in payload["groups"]] == [1, 2, 3]
    assert all(item["metrics"]["n"] == 2 for item in payload["groups"])
    assert payload["rows_included"] == 6


def test_describe_variable_reports_observed_numeric_summary(service):
    payload = service.describe_variable("animals.csv", "weight_g")

    assert payload["profile"]["dtype"] == "integer"
    assert payload["summary"]["count"] == 48
    assert payload["summary"]["n_missing"] == 0
    assert payload["summary"]["min"] == 18
    assert payload["summary"]["max"] == 30
    assert payload["summary"]["mean"] == pytest.approx(24.020833333333332)


def test_join_tables_uses_explicit_keys_and_reports_cardinality(service):
    payload = service.join_tables(
        "animals.csv",
        "observations.csv",
        left_keys=["animal_id"],
        right_keys=["animal_id"],
        left_columns=["animal_id", "genotype"],
        right_columns=["observation_id", "animal_id", "session"],
        limit=5,
    )

    assert payload["diagnostics"]["cardinality"] == "one_to_many"
    assert payload["diagnostics"]["left_duplicate_keys"] == 0
    assert payload["diagnostics"]["right_duplicate_keys"] == 48
    assert payload["total_result_rows"] == 144
    assert payload["returned"] == 5
    assert payload["truncated"] is True
    assert payload["rows"][0]["left"]["animal_id"] == "A001"
    assert payload["rows"][0]["right"]["observation_id"] == "OBS0001"
    assert payload["rows"][0]["source_rows"] == {"left": 2, "right": 2}


def test_relationships_remain_undetermined_until_sidecar_exists(service):
    listing = service.list_relationships()
    assert listing["determined"] is False
    assert listing["relationships"] == []
    assert service.dataset_inventory()["relationships_determined"] is False


def test_structural_overlap_is_candidate_not_promoted(service):
    bundle = service.build_relationships()

    assert bundle["determined"] is True
    assert bundle["status_counts"] == {"candidate": 1}
    relation = bundle["relationships"][0]
    assert relation["status"] == "candidate"
    assert relation["left"]["table"] == "animals.csv"
    assert relation["right"]["table"] == "observations.csv"
    assert relation["left"]["keys"] == ["animal_id"]
    assert relation["right"]["keys"] == ["animal_id"]
    assert relation["cardinality"] == "one_to_many"
    assert relation["matched_distinct_keys"] == 48
    assert relation["joined_row_count"] == 144
    assert relation["evidence"]["examples"]
    # Runtime discovery alone does not mutate the output contract.
    assert service.dataset_inventory()["relationships_determined"] is False


def test_declared_relationship_can_drive_named_join(ingested):
    builder = DatasetService(ingested.output_dir, load_relationships=False)
    bundle = builder.build_relationships(
        [
            {
                "left": "animals.csv",
                "right": "observations.csv",
                "left_keys": ["animal_id"],
                "right_keys": ["animal_id"],
                "expected_cardinality": "one_to_many",
                "note": "animal registry to repeated observations",
            }
        ]
    )
    (ingested.output_dir / "relationships.json").write_text(
        json.dumps(bundle, indent=2) + "\n", encoding="utf-8"
    )

    service = DatasetService(ingested.output_dir)
    inventory = service.dataset_inventory()
    assert inventory["relationships_determined"] is True
    relation = service.list_relationships(status="declared")["relationships"][0]
    assert relation["status"] == "declared"
    assert relation["cardinality"] == "one_to_many"

    joined = service.join_relationship(
        relation["id"],
        left_columns=["animal_id", "genotype"],
        right_columns=["observation_id", "session"],
        limit=4,
    )
    assert joined["total_result_rows"] == 144
    assert joined["returned"] == 4
    assert joined["relationship_contract"]["id"] == relation["id"]
    assert joined["relationship_contract"]["status"] == "declared"


def test_composite_declared_relationship_is_representable(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "subjects.csv").write_text(
        "batch,local_id,group\n1,A,control\n1,B,test\n2,A,test\n",
        encoding="utf-8",
    )
    (source / "sessions.csv").write_text(
        "batch,local_id,session\n1,A,1\n1,A,2\n1,B,1\n2,A,1\n",
        encoding="utf-8",
    )
    result = ingest(source, tmp_path / "out")
    service = DatasetService(result.output_dir, load_relationships=False)

    bundle = service.build_relationships(
        [
            {
                "left": "subjects.csv",
                "right": "sessions.csv",
                "left_keys": ["batch", "local_id"],
                "right_keys": ["batch", "local_id"],
                "expected_cardinality": "one_to_many",
            }
        ]
    )

    relation = bundle["relationships"][0]
    assert relation["status"] == "declared"
    assert relation["left"]["keys"] == ["batch", "local_id"]
    assert relation["right"]["keys"] == ["batch", "local_id"]
    assert relation["cardinality"] == "one_to_many"
    assert relation["matched_distinct_keys"] == 3
    assert relation["joined_row_count"] == 4


def test_cardinality_violation_rejects_declaration_and_named_join(ingested):
    builder = DatasetService(ingested.output_dir, load_relationships=False)
    bundle = builder.build_relationships(
        [
            {
                "left": "animals.csv",
                "right": "observations.csv",
                "left_keys": ["animal_id"],
                "right_keys": ["animal_id"],
                "expected_cardinality": "one_to_one",
            }
        ]
    )
    relation = bundle["relationships"][0]
    assert relation["status"] == "rejected"
    assert "observed cardinality" in relation["rejection_reasons"][0]

    (ingested.output_dir / "relationships.json").write_text(
        json.dumps(bundle, indent=2) + "\n", encoding="utf-8"
    )
    service = DatasetService(ingested.output_dir)
    with pytest.raises(ValueError, match="only declared or deterministic"):
        service.join_relationship(relation["id"])


def test_saved_relationship_assertions_are_withheld_after_source_drift(
    dataset_copy: Path, tmp_path: Path
):
    result = ingest(dataset_copy, tmp_path / "out")
    builder = DatasetService(result.output_dir, load_relationships=False)
    bundle = builder.build_relationships(
        [
            {
                "left": "animals.csv",
                "right": "observations.csv",
                "left_keys": ["animal_id"],
                "right_keys": ["animal_id"],
                "expected_cardinality": "one_to_many",
            }
        ]
    )
    (result.output_dir / "relationships.json").write_text(
        json.dumps(bundle, indent=2) + "\n", encoding="utf-8"
    )

    service = DatasetService(result.output_dir)
    assert service.list_relationships()["total"] == 1

    (dataset_copy / "observations.csv").write_text(
        "observation_id,animal_id\nOBS1,A001\n", encoding="utf-8"
    )
    listing = service.list_relationships()
    assert listing["determined"] is True
    assert listing["relationships"] == []
    assert listing["source_integrity"]["matches"] is False
    assert "regenerate relationships" in listing["content_withheld"]


def test_stale_relationship_sidecar_is_refused(ingested):
    (ingested.output_dir / "relationships.json").write_text(
        json.dumps(
            {
                "relationships_version": "0.1.0",
                "dataset_id": "sha256:" + "9" * 64,
                "determined": True,
                "relationship_count": 0,
                "status_counts": {},
                "relationships": [],
                "skipped": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(OutputError, match="regenerate relationships"):
        DatasetService(ingested.output_dir)


def test_aggregate_rejects_numeric_metrics_on_string_columns(service):
    with pytest.raises(ValueError, match="requires a numeric column"):
        service.aggregate(
            "animals.csv",
            metrics=[{"op": "mean", "column": "genotype"}],
        )


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


def test_dataset_inventory_surfaces_the_ingest_timestamp(service, ingested):
    """The manifest stays timestamp-free; the timestamp is still reachable."""
    summary = service.dataset_inventory()
    assert summary["ingested_at"] == ingested.provenance["started_at"]
    assert summary["ingested_at"].endswith("Z")
    assert summary["ingest_duration_s"] >= 0
    assert summary["tool_version"]
    assert "ingested_at" not in ingested.manifest


def test_get_provenance_returns_the_run_not_the_dataset(service, ingested):
    provenance = service.get_provenance()
    assert provenance["dataset_id"] == ingested.dataset_id
    assert provenance["started_at"] and provenance["finished_at"]
    assert provenance["duration_seconds"] >= 0
    assert provenance["source_verified_unchanged"] is True
    assert provenance["tool"]["name"] == "data2agent"


def test_dataset_inventory_reports_the_missing_value_convention(service):
    convention = service.dataset_inventory()["missing_value_convention"]
    assert convention["id"] == "default-sentinels"
    assert convention["ambiguous_tokens_resolved"] is False


def test_validate_identifier_checks_syntax_and_says_so(ingested):
    service = DatasetService(ingested.output_dir, mode="fair-deterministic")

    valid = service.validate_identifier("10.5281/zenodo.0000000")
    assert valid["syntactically_valid"] is True
    assert valid["schemes"] == ["doi"]
    assert valid["resolves"] is None and valid["resolution_attempted"] is False
    assert valid["occurrences"], "the identifier occurs in this dataset"

    assert service.validate_identifier("not-an-identifier")["syntactically_valid"] is False
    # A well-formed ORCID with a bad check digit is caught without any network call.
    assert service.validate_identifier("0000-0002-1825-0098")["checksum_valid"] is False


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


def test_read_rows_withholds_observations_when_the_backing_file_drifts(
    dataset_copy: Path, tmp_path: Path
):
    result = ingest(dataset_copy, tmp_path / "out")
    service = DatasetService(result.output_dir)

    assert service.read_rows("animals.csv", limit=1)["returned"] == 1

    (dataset_copy / "animals.csv").write_text("animal_id\nA001\n", encoding="utf-8")
    payload = service.read_rows("animals.csv", limit=1)

    assert payload["integrity"]["matches"] is False
    assert payload["rows"] == []
    assert payload["returned"] == 0
    assert "re-ingest" in payload["content_withheld"]


def test_paths_cannot_escape_the_dataset_root(service):
    with pytest.raises(KeyError):
        service.inspect_file("../../etc/passwd")


def test_resources_are_served_as_json_text(service):
    for uri in (
        "dataset://manifest",
        "dataset://provenance",
        "dataset://evidence",
        "dataset://metadata",
        "dataset://relationships",
        "dataset://files/animals.csv",
    ):
        assert service.resource(uri).strip().startswith("{")
    with pytest.raises(KeyError):
        service.resource("dataset://nope")


def test_service_needs_an_ingested_directory(tmp_path: Path):
    with pytest.raises(OutputError, match="run `data2agent ingest` first"):
        DatasetService(tmp_path)


def test_mismatched_dataset_ids_are_refused(ingested):
    """An interrupted or half-overwritten output directory would otherwise serve
    one dataset's evidence under another's id -- a wrong answer shaped exactly
    like a right one."""
    evidence_path = ingested.output_dir / "evidence.json"
    payload = json.loads(evidence_path.read_text())
    payload["dataset_id"] = "sha256:" + "0" * 64
    evidence_path.write_text(json.dumps(payload))

    with pytest.raises(OutputError, match="does not describe a single dataset"):
        DatasetService(ingested.output_dir)


def test_mismatched_provenance_id_is_refused(ingested):
    provenance_path = ingested.output_dir / "provenance.json"
    payload = json.loads(provenance_path.read_text())
    payload["dataset_id"] = "sha256:" + "1" * 64
    provenance_path.write_text(json.dumps(payload))

    with pytest.raises(OutputError, match="does not describe a single dataset"):
        DatasetService(ingested.output_dir)


def test_a_preview_reads_only_the_bytes_it_returns(ingested, monkeypatch):
    """read_bytes()[:limit] pulled the whole file into memory first, so a 4 KiB
    preview of a multi-gigabyte file could take the server down."""
    service = DatasetService(ingested.output_dir)
    source = service.source_dir / "animals.csv"
    real_open = Path.open
    reads: list[int | None] = []

    def recording_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == source:
            real_read = handle.read

            def read(size=-1):
                reads.append(size)
                return real_read(size)

            handle.read = read
        return handle

    monkeypatch.setattr(Path, "open", recording_open)
    payload = service.inspect_file("animals.csv", preview_bytes=64)

    assert payload["preview_bytes"] == 64
    assert payload["preview_truncated"] is True
    assert 64 in reads, "the preview must be read with a bounded read(limit)"
    assert -1 not in reads, "the whole file must never be pulled into memory for a preview"


def test_a_preview_larger_than_the_file_is_not_truncated(ingested):
    service = DatasetService(ingested.output_dir)
    payload = service.inspect_file("dataset_description.json", preview_bytes=1_000_000)
    assert payload["preview_truncated"] is False
    assert payload["preview_bytes"] == payload["size"]
