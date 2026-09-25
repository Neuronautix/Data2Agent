"""aggregate_join through a declared identifier crosswalk.

Every identifier is synthetic. The shape mirrors the real failure the crosswalk
exists for: a registry splits an animal ID into cage and tail columns and
writes the cage with a dash, while the measurement sheet writes one
concatenated ID without it -- and the measurement sheet has repeated time bins,
so a per-animal reduction must group by the canonical animal, not by either
file's spelling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data2agent.ingest import ingest
from data2agent.mcp import DatasetService
from data2agent.relationships import load_crosswalk_file

REGISTRY = "cage,tail,group\nZ-7,L,ctrl\nZ-7,R,ctrl\nZ-8,L,test\nZ-9,L,ctrl\n"
BINS = (
    "animal,day,bin,dur\n"
    "Z7-L,1,1,10\n"
    "Z7-L,1,2,20\n"
    "Z7-L,1,3,30\n"
    "Z7-R,1,1,5\n"
    "Z8-L,1,1,4\n"
    "Z8-L,1,2,2\n"
    "Q5-X,1,1,99\n"
)
CROSSWALK = (
    "canonical_id,form\n"
    "Z-7_L,Z-7-L\n"
    "Z-7_L,Z7-L\n"
    "Z-7_R,Z-7-R\n"
    "Z-7_R,Z7-R\n"
    "Z-8_L,Z-8-L\n"
    "Z-8_L,Z8-L\n"
    "Z-9_L,Z-9-L\n"
)
DECLARATION = {
    "left": "registry.csv",
    "right": "bins.csv",
    "left_keys": ["cage", "tail"],
    "right_keys": ["animal"],
    "left_key_format": "{cage}-{tail}",
    "key_crosswalk": "ids",
    "expected_cardinality": "one_to_many",
}
PER_ANIMAL = {
    "unit": ["key.canonical_id", "right.day"],
    "unit_metrics": [{"op": "sum", "column": "right.dur", "name": "total"}],
    "metrics": [{"op": "mean", "column": "total"}, {"op": "count", "name": "n_animals"}],
}


def _dataset(tmp_path: Path, bins: str = BINS) -> tuple[Path, dict]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "registry.csv").write_text(REGISTRY, encoding="utf-8")
    (source / "bins.csv").write_text(bins, encoding="utf-8")
    crosswalk_path = tmp_path / "ids.csv"
    crosswalk_path.write_text(CROSSWALK, encoding="utf-8")
    output = ingest(source, tmp_path / "out").output_dir
    bundle = DatasetService(output, load_relationships=False).build_relationships(
        [DECLARATION], crosswalks=[load_crosswalk_file(crosswalk_path, name="ids")]
    )
    (output / "relationships.json").write_text(json.dumps(bundle), encoding="utf-8")
    return output, bundle


def _declared_id(bundle: dict) -> str:
    (relation,) = [
        record
        for record in bundle["relationships"]
        if record["status"] == "declared" and record.get("key_mapping", {}).get("crosswalk")
    ]
    return relation["id"]


def _groups(payload: dict, column: str) -> dict:
    return {item["group"][column]: item for item in payload["groups"]}


def test_named_relationship_aggregates_per_canonical_animal(tmp_path: Path):
    output, bundle = _dataset(tmp_path)
    relationship_id = _declared_id(bundle)
    payload = DatasetService(output).aggregate_join(
        relationship_id=relationship_id, group_by=["left.group"], **PER_ANIMAL
    )

    ctrl = _groups(payload, "left.group")["ctrl"]
    # Z-7_L sums three bins (60), Z-7_R one (5): the per-animal mean is 32.5,
    # where a row mean over the four ctrl bins would be 16.25.
    assert ctrl["metrics"] == {"mean:total": 32.5, "n_animals": 2}
    assert (ctrl["n_units"], ctrl["n_rows"]) == (2, 4)
    first = ctrl["units"][0]
    assert first["unit"] == {"key.canonical_id": "Z-7_L", "right.day": 1}
    # The canonical unit shows every raw spelling it was assembled from.
    assert first["distinct_values"] == {"key.left_form": ["Z-7-L"], "key.right_form": ["Z7-L"]}
    assert _groups(payload, "left.group")["test"]["metrics"]["mean:total"] == 6

    # The join is the relationship's own: its key format and its crosswalk, cited.
    crosswalk = payload["relationship_contract"]["crosswalk"]
    assert crosswalk["name"] == "ids" and len(crosswalk["sha256"]) == 64
    assert payload["key_mapping"]["crosswalk"]["sha256"] == crosswalk["sha256"]
    assert payload["operation"]["join"]["left_key_format"] == "{cage}-{tail}"
    assert payload["operation"]["join"]["crosswalk"] == "ids"
    assert payload["aggregation_key_mapping"] == {
        "rows": 6,
        "mapped_rows": 6,
        "unmapped_rows": 0,
        "rows_with_missing_key": 0,
        "unmatched_rows": 0,
        "mapped_distinct_canonical_ids": 3,
    }
    # The measurement side's unmapped spelling is reported on the table facts.
    assert payload["key_mapping"]["right"]["unmapped_rows"] == 1
    assert json.loads(json.dumps(payload, allow_nan=False)) == payload


def test_named_aggregation_matches_join_relationship(tmp_path: Path):
    output, bundle = _dataset(tmp_path)
    relationship_id = _declared_id(bundle)
    service = DatasetService(output)
    page = service.join_relationship(relationship_id, limit=1000)
    counted = service.aggregate_join(relationship_id=relationship_id, metrics=[{"op": "count"}])
    assert counted["groups"][0]["metrics"]["count"] == page["total_result_rows"] == 6
    assert counted["key_mapping"] == page["key_mapping"]


def test_explicit_spec_with_a_declared_crosswalk_counts_unmapped_rows(tmp_path: Path):
    output, _ = _dataset(tmp_path)
    payload = DatasetService(output).aggregate_join(
        left="bins.csv",
        right="registry.csv",
        left_keys=["animal"],
        right_keys=["cage", "tail"],
        right_key_format="{cage}-{tail}",
        crosswalk="ids",
        how="left",
        group_by=["right.group"],
        unit=["key.canonical_id", "left.day"],
        unit_metrics=[{"op": "sum", "column": "left.dur", "name": "total"}],
        metrics=[{"op": "mean", "column": "total"}],
    )
    assert payload["join"]["cardinality"] == "many_to_one"
    assert payload["aggregation_key_mapping"]["unmapped_rows"] == 1
    assert payload["aggregation_key_mapping"]["unmatched_rows"] == 1
    assert payload["aggregation_key_mapping"]["mapped_rows"] == 6
    # The unmapped animal has no canonical ID, so it is not attributed to a unit.
    missing = payload["analysis_unit"]["rows_with_missing_unit_key"]
    assert missing["count"] == 1 and missing["source_rows"] == [{"left": 8, "right": None}]
    assert any("absent from the crosswalk" in item for item in payload["join"]["warnings"])
    assert _groups(payload, "right.group")["ctrl"]["metrics"]["mean:total"] == 32.5


def test_an_undeclared_crosswalk_name_is_refused(tmp_path: Path):
    output, _ = _dataset(tmp_path)
    with pytest.raises(KeyError, match="no crosswalk named 'mine'"):
        DatasetService(output).aggregate_join(
            left="bins.csv",
            right="registry.csv",
            left_keys=["animal"],
            right_keys=["cage", "tail"],
            right_key_format="{cage}-{tail}",
            crosswalk="mine",
            metrics=[{"op": "count"}],
        )


def test_a_crosswalk_collision_refuses_the_aggregation(tmp_path: Path):
    # One sheet writes Z-7_L in two different forms: the crosswalk says they are
    # one animal, but only a person can say whether that is true here.
    output, bundle = _dataset(tmp_path, bins=BINS + "Z-7-L,1,4,40\n")
    relationship_id = next(
        record["id"]
        for record in bundle["relationships"]
        if record.get("key_mapping", {}).get("crosswalk")
    )
    service = DatasetService(output)
    payload = service.aggregate_join(
        left="registry.csv",
        right="bins.csv",
        left_keys=["cage", "tail"],
        right_keys=["animal"],
        left_key_format="{cage}-{tail}",
        crosswalk="ids",
        group_by=["left.group"],
        **PER_ANIMAL,
    )
    assert payload["groups"] == []
    assert "crosswalk collision on right" in payload["content_withheld"]
    assert payload["key_mapping"]["right"]["collisions"][0]["canonical_id"] == "Z-7_L"
    # By name it never gets that far: the same collision rejected the
    # declaration when relationships were built, and a rejected record cannot
    # drive a join.
    record = service.get_relationship(relationship_id)["relationship"]
    assert record["status"] == "rejected"
    with pytest.raises(ValueError, match="only declared or deterministic"):
        service.aggregate_join(relationship_id=relationship_id, metrics=[{"op": "count"}])


def test_a_rendering_collision_refuses_the_aggregation(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    # ("1", "23") and ("12", "3") both render "123" under "{a}{b}".
    (source / "left.csv").write_text("a,b,g\n1,23,x\n12,3,y\n", encoding="utf-8")
    (source / "right.csv").write_text("id,v\n123,1\n", encoding="utf-8")
    output = ingest(source, tmp_path / "out").output_dir
    payload = DatasetService(output, load_relationships=False).aggregate_join(
        left="left.csv",
        right="right.csv",
        left_keys=["a", "b"],
        right_keys=["id"],
        left_key_format="{a}{b}",
        metrics=[{"op": "count"}],
    )
    assert payload["groups"] == []
    assert "rendering collision on left" in payload["content_withheld"]


def test_key_pseudo_columns_need_a_resolved_join(tmp_path: Path):
    output, _ = _dataset(tmp_path)
    service = DatasetService(output)
    with pytest.raises(ValueError, match="resolves its keys"):
        service.aggregate_join(
            left="bins.csv",
            right="bins.csv",
            left_keys=["animal"],
            right_keys=["animal"],
            unit=["key.canonical_id"],
            metrics=[{"op": "count"}],
        )
    with pytest.raises(ValueError, match="declared crosswalk"):
        service.aggregate_join(
            left="bins.csv",
            right="registry.csv",
            left_keys=["animal"],
            right_keys=["cage", "tail"],
            right_key_format="{cage}-{tail}",
            unit=["key.canonical_id"],
            metrics=[{"op": "count"}],
        )
    with pytest.raises(ValueError, match="unknown key pseudo-column"):
        service.aggregate_join(
            left="bins.csv",
            right="registry.csv",
            left_keys=["animal"],
            right_keys=["cage", "tail"],
            right_key_format="{cage}-{tail}",
            crosswalk="ids",
            unit=["key.animal"],
            metrics=[{"op": "count"}],
        )


def test_a_relationship_join_cannot_be_overridden(tmp_path: Path):
    output, bundle = _dataset(tmp_path)
    with pytest.raises(ValueError, match="not both"):
        DatasetService(output).aggregate_join(
            relationship_id=_declared_id(bundle), crosswalk="ids", metrics=[{"op": "count"}]
        )
