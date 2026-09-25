"""Declared ``*_forms_per_canonical``: several written forms of one subject per table.

Every identifier here is synthetic. The shape mirrors a behaviour-scoring
export in which each observation id (one per session day) names the same
animal: the crosswalk maps every observation id to that animal, so one table
legitimately writes one canonical ID in several forms. By default that is a
collision and rejects the relationship; declaring the side "many" accepts it
while still reporting every collision in full.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data2agent.ingest import ingest
from data2agent.mcp import DatasetService
from data2agent.relationships import load_crosswalk_file

REGISTRY = "cage,tail,group\nZ-7,L,ctrl\nZ-8,L,test\n"
SCORING = "obs,bin,dur\nD1_Z7L,1,10\nD1_Z7L,2,20\nD2_Z7L,1,30\nD1_Z8L,1,4\n"
CROSSWALK = (
    "canonical_id,form\nZ-7_L,Z-7-L\nZ-7_L,D1_Z7L\nZ-7_L,D2_Z7L\nZ-8_L,Z-8-L\nZ-8_L,D1_Z8L\n"
)
BASE = {
    "left": "registry.csv",
    "right": "scoring.csv",
    "left_keys": ["cage", "tail"],
    "right_keys": ["obs"],
    "left_key_format": "{cage}-{tail}",
    "key_crosswalk": "ids",
    "expected_cardinality": "one_to_many",
}


def _build(tmp_path: Path, declaration: dict, *, registry: str = REGISTRY):
    source = tmp_path / "source"
    source.mkdir()
    (source / "registry.csv").write_text(registry, encoding="utf-8")
    (source / "scoring.csv").write_text(SCORING, encoding="utf-8")
    crosswalk_path = tmp_path / "ids.csv"
    crosswalk_path.write_text(CROSSWALK, encoding="utf-8")
    output = ingest(source, tmp_path / "out").output_dir
    bundle = DatasetService(output, load_relationships=False).build_relationships(
        [declaration], crosswalks=[load_crosswalk_file(crosswalk_path, name="ids")]
    )
    (output / "relationships.json").write_text(json.dumps(bundle), encoding="utf-8")
    (relation,) = [
        record
        for record in bundle["relationships"]
        if record["basis"]["method"] == "explicit-declaration"
    ]
    return output, bundle, relation


def test_default_one_still_rejects_several_forms_in_one_table(tmp_path: Path):
    _, _, relation = _build(tmp_path, BASE)
    assert relation["status"] == "rejected"
    assert any("crosswalk collision on the right side" in r for r in relation["rejection_reasons"])
    assert relation["key_mapping"]["right"]["forms_per_canonical"] == "one"


def test_many_accepts_and_still_reports_every_collision(tmp_path: Path):
    _, _, relation = _build(tmp_path, {**BASE, "right_forms_per_canonical": "many"})

    assert relation["status"] == "declared"
    assert "rejection_reasons" not in relation
    mapping = relation["key_mapping"]
    assert mapping["left"]["forms_per_canonical"] == "one"
    assert mapping["right"]["forms_per_canonical"] == "many"
    assert mapping["right"]["collisions"] == [
        {
            "canonical_id": "Z-7_L",
            "forms": [
                {"form": "D1_Z7L", "rows": 2, "source_rows": [2, 3]},
                {"form": "D2_Z7L", "rows": 1, "source_rows": [4]},
            ],
        }
    ]
    assert any("as declared (forms_per_canonical 'many')" in w for w in relation["warnings"])

    # Canonical level: two animals, one registry row each, four scoring rows.
    assert relation["cardinality"] == "one_to_many"
    assert mapping["cardinality_level"] == "canonical_id"
    assert relation["matched_distinct_keys"] == 2
    assert relation["right"]["distinct_keys"] == 2
    assert relation["joined_row_count"] == 4
    # Form level: three observation ids for those two animals, one repeated.
    assert mapping["right"]["form_level"] == {
        "distinct_forms": 3,
        "forms_unique": False,
        "canonical_ids_with_several_forms": 1,
        "max_forms_per_canonical_id": 2,
    }
    assert mapping["left"]["form_level"]["forms_unique"] is True
    assert mapping["form_level_cardinality"] == "one_to_many"


def test_named_join_cites_the_option_and_keeps_raw_forms(tmp_path: Path):
    output, _, relation = _build(tmp_path, {**BASE, "right_forms_per_canonical": "many"})
    joined = DatasetService(output).join_relationship(relation["id"], limit=10)

    contract = joined["relationship_contract"]
    assert contract["forms_per_canonical"] == {"left": "one", "right": "many"}
    assert contract["cardinality_level"] == "canonical_id"
    assert joined["operation"]["forms_per_canonical"] == {"left": "one", "right": "many"}
    assert joined["total_result_rows"] == 4
    forms = sorted({row["key"]["right_raw"][0] for row in joined["rows"]})
    assert forms == ["D1_Z7L", "D1_Z8L", "D2_Z7L"]
    assert {row["key"]["canonical_id"] for row in joined["rows"]} == {"Z-7_L", "Z-8_L"}
    # Collisions stay visible on the join itself.
    assert joined["key_mapping"]["right"]["collisions"][0]["canonical_id"] == "Z-7_L"


def test_per_animal_aggregation_pools_the_forms(tmp_path: Path):
    output, _, relation = _build(tmp_path, {**BASE, "right_forms_per_canonical": "many"})
    payload = DatasetService(output).aggregate_join(
        relationship_id=relation["id"],
        group_by=["left.group"],
        unit=["key.canonical_id"],
        unit_metrics=[{"op": "sum", "column": "right.dur", "name": "total"}],
        metrics=[{"op": "mean", "column": "total"}, {"op": "count", "name": "n_animals"}],
    )
    groups = {item["group"]["left.group"]: item for item in payload["groups"]}
    ctrl = groups["ctrl"]
    # Z-7_L's two observation ids (10 + 20 and 30) pool into one animal of 60.
    assert ctrl["metrics"] == {"mean:total": 60, "n_animals": 1}
    assert (ctrl["n_units"], ctrl["n_rows"]) == (1, 3)
    assert ctrl["units"][0]["distinct_values"]["key.right_form"] == ["D1_Z7L", "D2_Z7L"]
    assert groups["test"]["metrics"] == {"mean:total": 4, "n_animals": 1}
    assert payload["relationship_contract"]["forms_per_canonical"]["right"] == "many"
    assert payload["operation"]["join"]["forms_per_canonical"] == {"left": "one", "right": "many"}


def test_many_does_not_excuse_a_rendering_collision(tmp_path: Path):
    registry = "cage,tail,group\nc1,23,ctrl\nc12,3,test\n"
    declaration = {
        **BASE,
        "left_key_format": "{cage}{tail}",
        "left_forms_per_canonical": "many",
        "right_forms_per_canonical": "many",
    }
    output, _, relation = _build(tmp_path, declaration, registry=registry)
    assert relation["status"] == "rejected"
    assert any("rendering collision on the left side" in r for r in relation["rejection_reasons"])
    adhoc = DatasetService(output).join_tables(
        "registry.csv",
        "scoring.csv",
        left_keys=["cage", "tail"],
        right_keys=["obs"],
        left_key_format="{cage}{tail}",
        crosswalk="ids",
        forms_per_canonical={"left": "many", "right": "many"},
    )
    assert adhoc["rows"] == []
    assert "rendering collision on left" in adhoc["content_withheld"]


def test_many_on_the_other_side_has_no_effect(tmp_path: Path):
    _, _, relation = _build(tmp_path, {**BASE, "left_forms_per_canonical": "many"})
    assert relation["status"] == "rejected"
    assert any("crosswalk collision on the right side" in r for r in relation["rejection_reasons"])


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"right_forms_per_canonical": "several"}, "must be 'one' or 'many'"),
        ({"forms_per_canonical": "many"}, "declared per side"),
    ],
)
def test_the_option_is_validated(tmp_path: Path, extra: dict, message: str):
    with pytest.raises(ValueError, match=message):
        _build(tmp_path, {**BASE, **extra})


def test_many_without_a_crosswalk_is_refused(tmp_path: Path):
    declaration = {key: value for key, value in BASE.items() if key != "key_crosswalk"}
    with pytest.raises(ValueError, match="applies only through a key_crosswalk"):
        _build(tmp_path, {**declaration, "right_forms_per_canonical": "many"})


def test_bundle_with_the_option_matches_its_schema(tmp_path: Path):
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "relationships.schema.json"
    _, bundle, _ = _build(tmp_path, {**BASE, "right_forms_per_canonical": "many"})
    assert bundle["relationships_version"] == "0.3.0"
    jsonschema.validate(bundle, json.loads(schema_path.read_text(encoding="utf-8")))
