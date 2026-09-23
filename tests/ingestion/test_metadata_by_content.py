"""Metadata recognised by content, not only by filename convention (D2A-49a).

The rule these tests pin down is deliberately narrow. Half of them are negative:
a false positive here corrupts five FAIR indicators at once, so "this shape is
NOT metadata" is as much the contract as "this shape is".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data2agent.ingest import metadata
from data2agent.ingest.pipeline import ingest


def _registry_rows(count: int = 12) -> str:
    """A subject registry: one row per animal, plus grouping attributes."""
    header = "animal_id,batch,sex,group\n"
    body = "".join(
        f"A{index:03d},{index % 3 + 1},{'MF'[index % 2]},{'control' if index % 2 else 'treated'}\n"
        for index in range(count)
    )
    return header + body


def _measurement_rows(animals: int = 12, sessions: int = 3) -> str:
    """A measurement table: the same animal on several rows."""
    header = "animal_id,session,latency_s\n"
    body = "".join(
        f"A{animal:03d},{session},{12.5 + animal * 0.25 + session}\n"
        for animal in range(animals)
        for session in range(1, sessions + 1)
    )
    return header + body


def _dataset(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "dataset"
    root.mkdir(parents=True)
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


def _ingest(tmp_path: Path, files: dict[str, str], name: str = "out"):
    return ingest(_dataset(tmp_path / name, files), tmp_path / name / "result")


def _by_path(manifest: dict) -> dict[str, dict]:
    return {item["path"]: item for item in manifest["metadata_files"]}


# -- recognition by content -------------------------------------------------


def test_a_registry_table_under_an_unconventional_name_is_recognised(tmp_path: Path):
    """The XP14 case: an animal registry whose filename matches no convention."""
    result = _ingest(tmp_path, {"IDs Batch Sex Group.csv": _registry_rows()})
    entry = _by_path(result.manifest)["IDs Batch Sex Group.csv"]

    assert entry["convention"] == "keyed-registry-table"
    assert entry["recognised_by"] == "content"
    assert entry["basis"]["key_column"] == "animal_id"
    assert entry["basis"]["grouping_columns"] == ["batch", "sex", "group"]
    assert entry["basis"]["rows"] == 12


def test_a_json_descriptor_under_an_unconventional_name_is_recognised(tmp_path: Path):
    document = {
        "@context": "https://schema.org/",
        "@type": "Dataset",
        "name": "Study",
        "creator": {"name": "A Person"},
    }
    result = _ingest(tmp_path, {"study-info.json": json.dumps(document)})
    entry = _by_path(result.manifest)["study-info.json"]

    assert entry["convention"] == "schema-org"
    assert entry["recognised_by"] == "content"
    assert entry["basis"]["declared_by"] == "@context"


@pytest.mark.parametrize(
    ("document", "convention"),
    [
        ({"BIDSVersion": "1.9.0", "Name": "x"}, "bids"),
        ({"resources": [{"path": "a.csv"}], "name": "x"}, "frictionless"),
        ({"types": {"resourceTypeGeneral": "Dataset"}}, "datacite"),
        ({"titles": [{"title": "x"}], "creators": [{"name": "y"}]}, "datacite"),
        ({"@context": "https://w3id.org/ro/crate/1.1/context"}, "ro-crate"),
    ],
)
def test_a_json_document_that_names_its_own_standard_is_recognised(
    tmp_path: Path, document: dict, convention: str
):
    result = _ingest(tmp_path, {"anything.json": json.dumps(document)}, name=convention)
    entry = _by_path(result.manifest)["anything.json"]
    assert entry["convention"] == convention
    assert entry["recognised_by"] == "content"


# -- the recognition basis is always on the record --------------------------


def test_every_entry_records_how_it_was_recognised_and_what_the_filename_said(tmp_path: Path):
    result = _ingest(
        tmp_path,
        {
            "README.md": "# A dataset\n",
            "registry.csv": _registry_rows(),
        },
    )
    entries = _by_path(result.manifest)

    named = entries["README.md"]
    assert named["recognised_by"] == "filename_convention"
    assert named["filename_convention"] == "readme"

    structural = entries["registry.csv"]
    assert structural["recognised_by"] == "content"
    # The filename verdict stays visible either way: 'recognised by content'
    # must never obscure 'and the name matched nothing'.
    assert structural["filename_convention"] is None


def test_a_content_recognition_is_bound_to_the_bytes_that_produced_it(tmp_path: Path):
    result = _ingest(tmp_path, {"registry.csv": _registry_rows()})
    claims = result.evidence.query(check="metadata.content-signature")

    assert len(claims) == 1
    item = claims[0].evidence[0]
    assert item.source == "registry.csv"
    assert item.source_sha256 == next(
        entry["sha256"] for entry in result.manifest["files"] if entry["path"] == "registry.csv"
    )
    assert item.result["signature"] == "keyed-registry-table"


def test_an_embedded_recognition_does_not_stop_the_file_being_a_data_file(tmp_path: Path):
    """A registry table is metadata AND payload. Only one of those may be lost."""
    result = _ingest(tmp_path, {"registry.csv": _registry_rows()})
    entry = _by_path(result.manifest)["registry.csv"]
    assert entry["kind"] == "embedded"
    assert entry["file"] == "registry.csv"


# -- what is deliberately NOT recognised ------------------------------------


def test_a_measurement_table_is_not_recognised(tmp_path: Path):
    """The subject id repeats down the rows, so the table is not keyed by it."""
    result = _ingest(tmp_path, {"trials.csv": _measurement_rows()})
    assert result.manifest["metadata_files"] == []


def test_a_table_whose_attributes_individuate_its_rows_is_not_recognised(tmp_path: Path):
    """A unique key is not enough: a per-subject measurement is still a measurement."""
    rows = "animal_id,weight_g\n" + "".join(f"A{i:03d},{20 + i}\n" for i in range(8))
    result = _ingest(tmp_path, {"weights.csv": rows})
    assert result.manifest["metadata_files"] == []


def test_a_table_with_two_identifier_columns_is_not_recognised(tmp_path: Path):
    """Two subject ids is a mapping between subjects, not a registry of one."""
    rows = "animal_id,donor_id,group\n" + "".join(
        f"A{i:03d},D{i:03d},{'ctl' if i % 2 else 'trt'}\n" for i in range(8)
    )
    result = _ingest(tmp_path, {"pairs.csv": rows})
    assert result.manifest["metadata_files"] == []


def test_a_table_with_a_blank_in_its_key_is_not_recognised(tmp_path: Path):
    rows = _registry_rows().replace("A005,", ",", 1)
    result = _ingest(tmp_path, {"registry.csv": rows})
    assert result.manifest["metadata_files"] == []


def test_a_bare_list_of_identifiers_is_not_recognised(tmp_path: Path):
    """No attribute column means nothing is described."""
    rows = "animal_id,note\n" + "".join(f"A{i:03d},x\n" for i in range(8))
    result = _ingest(tmp_path, {"ids.csv": rows})
    assert result.manifest["metadata_files"] == []


def test_a_software_package_descriptor_is_not_read_as_dataset_metadata(tmp_path: Path):
    """name/description/version/author describe a package as readily as a dataset."""
    document = {
        "name": "tooling",
        "version": "1.0.0",
        "description": "helpers",
        "license": "MIT",
        "author": "someone",
        "keywords": ["analysis"],
    }
    result = _ingest(tmp_path, {"tooling.json": json.dumps(document)})
    assert result.manifest["metadata_files"] == []


def test_a_json_ld_document_in_an_unknown_vocabulary_is_not_recognised(tmp_path: Path):
    result = _ingest(tmp_path, {"thing.json": json.dumps({"@context": "https://example.org/v1"})})
    assert result.manifest["metadata_files"] == []


def test_a_json_array_is_not_recognised(tmp_path: Path):
    result = _ingest(tmp_path, {"rows.json": json.dumps([{"BIDSVersion": "1.9.0"}])})
    assert result.manifest["metadata_files"] == []


def test_a_dataset_with_no_metadata_still_reports_an_empty_list(tmp_path: Path):
    result = _ingest(
        tmp_path,
        {"trials.csv": _measurement_rows(), "notes.json": json.dumps({"count": 3})},
    )
    assert result.manifest["metadata_files"] == []
    assert result.manifest["metadata_candidates"] == []


# -- candidates: recognised, not readable -----------------------------------


def test_a_workbook_nobody_could_open_is_registered_as_a_candidate(tmp_path: Path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    from data2agent.readers import workbook as workbook_reader

    root = tmp_path / "dataset"
    root.mkdir()
    book = openpyxl.Workbook()
    book.active.append(["animal_id", "group"])
    book.active.append(["A001", "control"])
    book.save(root / "registry.xlsx")

    monkeypatch.setattr(workbook_reader, "available", lambda: False)
    result = ingest(root, tmp_path / "out")

    assert result.manifest["metadata_files"] == []
    candidate = result.manifest["metadata_candidates"][0]
    assert candidate["path"] == "registry.xlsx"
    assert candidate["reason"] == "reader-unavailable"
    assert result.evidence.query(check="metadata.candidate")


def test_a_json_document_that_will_not_parse_is_registered_as_a_candidate(tmp_path: Path):
    result = _ingest(tmp_path, {"broken.json": '{"BIDSVersion": '})
    assert result.manifest["metadata_files"] == []
    candidate = result.manifest["metadata_candidates"][0]
    assert candidate["path"] == "broken.json"
    assert candidate["reason"] == "unparsed"


def test_a_candidate_is_never_counted_as_recognised_metadata(tmp_path: Path):
    result = _ingest(tmp_path, {"broken.json": "{oops"})
    paths = {item["path"] for item in result.manifest["metadata_files"]}
    assert "broken.json" not in paths


# -- determinism ------------------------------------------------------------


def test_a_manifest_carrying_the_new_fields_still_matches_its_schema(tmp_path: Path):
    jsonschema = pytest.importorskip("jsonschema", reason="schema validation needs 'dev'")
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "dataset-manifest.schema.json"

    result = _ingest(
        tmp_path,
        {
            "registry.csv": _registry_rows(),
            "study-info.json": json.dumps({"@context": "https://schema.org/", "@type": "Dataset"}),
            "broken.json": "{oops",
        },
    )
    jsonschema.validate(result.manifest, json.loads(schema_path.read_text(encoding="utf-8")))


def test_two_ingests_of_identical_bytes_recognise_identically(tmp_path: Path):
    files = {
        "IDs Batch Sex Group.csv": _registry_rows(),
        "study-info.json": json.dumps({"@context": "https://schema.org/", "@type": "Dataset"}),
        "trials.csv": _measurement_rows(),
        "broken.json": "{oops",
    }
    first = _ingest(tmp_path, files, name="first")
    second = _ingest(tmp_path, files, name="second")

    assert first.dataset_id == second.dataset_id
    assert json.dumps(first.manifest["metadata_files"], sort_keys=True) == json.dumps(
        second.manifest["metadata_files"], sort_keys=True
    )
    assert first.manifest["metadata_candidates"] == second.manifest["metadata_candidates"]


# -- the uniqueness observation the table rule rests on ----------------------


def test_uniqueness_is_observed_beyond_the_distinct_enumeration_cap(tmp_path: Path):
    """48 unique ids exceed the 25-value cap, so 'distinct' alone cannot settle it."""
    result = _ingest(tmp_path, {"registry.csv": _registry_rows(48)})
    column = next(
        item
        for item in result.manifest["tables"]["registry.csv"]["columns"]
        if item["name"] == "animal_id"
    )
    assert column["distinct_exact"] is False, "the precondition for this test has changed"
    assert column["unique"] is True


def test_uniqueness_is_only_asked_of_subject_identifier_columns(tmp_path: Path):
    result = _ingest(tmp_path, {"registry.csv": _registry_rows()})
    columns = {item["name"]: item for item in result.manifest["tables"]["registry.csv"]["columns"]}
    assert "unique" in columns["animal_id"]
    # Absent, not false: the question was never asked of a grouping column.
    assert "unique" not in columns["group"]


def test_a_repeated_key_is_observed_as_not_unique(tmp_path: Path):
    result = _ingest(tmp_path, {"trials.csv": _measurement_rows()})
    column = next(
        item
        for item in result.manifest["tables"]["trials.csv"]["columns"]
        if item["name"] == "animal_id"
    )
    assert column["unique"] is False


# -- the closed name list ---------------------------------------------------


@pytest.mark.parametrize("name", ["ID", "animal id", "Animal_ID", "subject", "Mouse #"])
def test_subject_identifier_names_are_matched_after_normalisation(name: str):
    assert metadata.is_subject_identifier_name(name)


@pytest.mark.parametrize("name", ["observation_id", "session_id", "run_id", "latency_s", "group"])
def test_an_event_key_is_not_a_subject_identifier(name: str):
    """'*_id' as a pattern would key events, and a table of events is data."""
    assert not metadata.is_subject_identifier_name(name)
