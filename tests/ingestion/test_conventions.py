"""Missing-value conventions: declared, default, overridden."""

from __future__ import annotations

import json
from pathlib import Path

from data2agent.ingest import ingest
from data2agent.ingest.conventions import (
    AMBIGUOUS,
    DEFAULT_CONVENTION,
    SENTINEL,
    VALUE,
    custom,
    from_datapackage,
)


def test_the_default_convention_resolves_common_sentinels():
    for token in ("NA", "na", "n/a", "NULL", "NaN", "None"):
        assert DEFAULT_CONVENTION.classify(token) == SENTINEL


def test_the_default_convention_refuses_the_ambiguous_ones():
    """These often mean absence, and often do not. That is exactly why we don't decide."""
    for token in ("unknown", "-", "?", ".", "not recorded"):
        assert DEFAULT_CONVENTION.classify(token) == AMBIGUOUS


def test_ordinary_values_are_values():
    for token in ("C57BL/6J", "0", "nanogram", "none of the above"):
        assert DEFAULT_CONVENTION.classify(token) == VALUE


def test_a_declared_convention_is_read_from_a_data_package(dataset_copy: Path, tmp_path: Path):
    """When a dataset states its own convention, nothing has to be assumed."""
    (dataset_copy / "datapackage.json").write_text(
        json.dumps(
            {
                "name": "preclinical-minimal",
                "resources": [
                    {
                        "name": "animals",
                        "path": "animals.csv",
                        "schema": {"missingValues": ["", "NA", "unknown"]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = ingest(dataset_copy, tmp_path / "out")

    convention = result.manifest["missing_value_convention"]
    assert convention["id"] == "declared"
    assert "datapackage.json" in convention["source"]
    assert set(convention["tokens"]) == {"na", "unknown"}


def test_a_declared_convention_can_resolve_a_normally_ambiguous_token(
    dataset_copy: Path, tmp_path: Path
):
    target = dataset_copy / "animals.csv"
    target.write_text(target.read_text().replace(",F,", ",unknown,", 1), encoding="utf-8")
    (dataset_copy / "datapackage.json").write_text(
        json.dumps({"missingValues": ["unknown"]}), encoding="utf-8"
    )
    result = ingest(dataset_copy, tmp_path / "out")

    sex = next(
        column
        for column in result.manifest["tables"]["animals.csv"]["columns"]
        if column["name"] == "sex"
    )
    assert sex["missing_sentinel"] == 1
    assert sex["missing"] == 13
    assert sex["ambiguous_tokens_seen"] == {}


def test_an_explicit_override_beats_the_declaration(dataset_copy: Path, tmp_path: Path):
    (dataset_copy / "datapackage.json").write_text(
        json.dumps({"missingValues": ["NA"]}), encoding="utf-8"
    )
    result = ingest(dataset_copy, tmp_path / "out", convention=custom(["C57BL/6J"]))
    convention = result.manifest["missing_value_convention"]
    assert convention["id"] == "custom"
    strain = next(
        column
        for column in result.manifest["tables"]["animals.csv"]["columns"]
        if column["name"] == "strain"
    )
    assert strain["missing_sentinel"] == 45
    assert strain["sentinel_tokens_seen"] == {"C57BL/6J": 45}


def test_a_data_package_without_a_declaration_yields_nothing():
    assert from_datapackage({"name": "x"}, path="datapackage.json") is None


def test_the_convention_changes_the_dataset_reading_but_not_its_identity(
    dataset_copy: Path, tmp_path: Path
):
    """Identity is a property of the bytes; the convention is a reading of them."""
    from data2agent.ingest.conventions import STRICT_CONVENTION

    default = ingest(dataset_copy, tmp_path / "a")
    strict = ingest(dataset_copy, tmp_path / "b", convention=STRICT_CONVENTION)

    assert default.dataset_id == strict.dataset_id
    assert default.manifest["tables"] != strict.manifest["tables"]
