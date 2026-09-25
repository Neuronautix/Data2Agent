"""aggregate(unit=...) and aggregate_join through the service, on a synthetic dataset.

The dataset is written here, in code: a long behaviour sheet with an unequal
number of time bins per animal and day, and a registry that alone knows each
animal's genotype -- the shape that makes a row-level mean wrong and a
single-table aggregate unable to group by genotype at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data2agent.ingest import ingest
from data2agent.mcp import DatasetService
from data2agent.mcp import service as service_module

BEHAVIOUR = """animal,day,bin,dur (s)
A,1,1,10
A,1,2,20
A,1,3,30
A,2,1,8
B,1,1,5
B,2,1,6
B,2,2,NA
C,1,1,4
C,1,2,2
D,1,1,
"""

REGISTRY = """animal,genotype,treatment
A,KO,drug
B,KO,drug
C,WT,drug
D,WT,drug
E,WT,vehicle
"""


def _write_dataset(root: Path, behaviour: str = BEHAVIOUR, registry: str = REGISTRY) -> Path:
    source = root / "source"
    source.mkdir()
    (source / "behaviour.csv").write_text(behaviour, encoding="utf-8")
    (source / "registry.csv").write_text(registry, encoding="utf-8")
    return source


@pytest.fixture
def service(tmp_path: Path) -> DatasetService:
    result = ingest(_write_dataset(tmp_path), tmp_path / "out")
    return DatasetService(result.output_dir, load_relationships=False)


def _join(**overrides):
    spec = {
        "left": "behaviour.csv",
        "right": "registry.csv",
        "left_keys": ["animal"],
        "right_keys": ["animal"],
    }
    spec.update(overrides)
    return spec


def _groups(payload, *names):
    return {tuple(item["group"][name] for name in names): item for item in payload["groups"]}


def test_aggregate_with_unit_reports_units_rows_and_definitions(service):
    payload = service.aggregate(
        "behaviour.csv",
        group_by=["day"],
        unit=["animal", "day"],
        unit_metrics=[{"op": "sum", "column": "dur (s)"}],
        metrics=[
            {"op": "mean", "column": "sum:dur (s)"},
            {"op": "sem", "column": "sum:dur (s)"},
            {"op": "count", "name": "n_animals"},
        ],
    )
    day1 = _groups(payload, "day")[(1,)]
    # Units A/1=60, B/1=5, C/1=6, D/1=null (its only bin is empty).
    assert day1["n_units"] == 4 and day1["n_rows"] == 7
    assert day1["metrics"]["n_animals"] == 4
    assert day1["metrics"]["mean:sum:dur (s)"] == pytest.approx((60 + 5 + 6) / 3)
    assert day1["unit_metric_missing"]["sum:dur (s)"]["units_null"] == 1
    assert payload["analysis_unit"]["unit"] == ["animal", "day"]
    assert payload["operation"]["unit_metrics"] == [{"op": "sum", "column": "dur (s)"}]
    assert set(payload["metric_definitions"]) == {"sum", "mean", "sem", "count"}
    assert "sqrt(n)" in payload["metric_definitions"]["sem"]
    assert payload["input"]["backing_sha256"]
    assert payload["input"]["integrity"]["matches"] is True


def test_aggregate_without_unit_keeps_its_row_level_contract(service):
    payload = service.aggregate(
        "behaviour.csv",
        group_by=["day"],
        metrics=[{"op": "count", "name": "n"}, {"op": "mean", "column": "dur (s)"}],
    )
    day1 = _groups(payload, "day")[(1,)]
    assert day1["metrics"]["n"] == 7
    # Row mean over the six present day-1 bins: A's three bins dominate it.
    assert day1["metrics"]["mean:dur (s)"] == pytest.approx((10 + 20 + 30 + 5 + 4 + 2) / 6)
    assert "analysis_unit" not in payload


def test_unit_metrics_without_a_unit_is_refused(service):
    with pytest.raises(ValueError, match="requires a unit"):
        service.aggregate(
            "behaviour.csv",
            unit_metrics=[{"op": "sum", "column": "dur (s)"}],
            metrics=[{"op": "count"}],
        )


def test_aggregate_join_groups_by_a_registry_column(service):
    payload = service.aggregate_join(
        **_join(),
        group_by=["right.genotype", "left.day"],
        unit=["left.animal", "left.day"],
        unit_metrics=[{"op": "sum", "column": "left.dur (s)", "name": "total"}],
        metrics=[
            {"op": "mean", "column": "total"},
            {"op": "sd", "column": "total"},
            {"op": "count", "name": "n"},
        ],
    )
    groups = _groups(payload, "right.genotype", "left.day")
    ko1 = groups[("KO", 1)]
    assert ko1["n_units"] == 2 and ko1["n_rows"] == 4
    assert ko1["metrics"]["mean:total"] == pytest.approx(32.5)
    assert ko1["metrics"]["sd:total"] == pytest.approx(38.890872965260115)
    wt1 = groups[("WT", 1)]
    assert wt1["metrics"]["n"] == 2
    assert wt1["metrics"]["sd:total"] is None
    assert "n=1" in wt1["null_reasons"]["sd:total"]
    ko2 = groups[("KO", 2)]
    assert [unit["unit"] for unit in ko2["units"]] == [
        {"left.animal": "A", "left.day": 2},
        {"left.animal": "B", "left.day": 2},
    ]
    # Every unit points at the exact rows of both files it came from.
    assert ko2["units"][1]["source_rows"] == [{"left": 7, "right": 3}, {"left": 8, "right": 3}]
    assert ko2["units"][1]["rows_missing"] == {"total": "partial"}


def test_aggregate_join_cites_both_files_and_the_join(service):
    payload = service.aggregate_join(
        **_join(how="left"), group_by=["right.genotype"], metrics=[{"op": "count"}]
    )
    assert payload["operation"]["join"] == {
        "left": "behaviour.csv",
        "right": "registry.csv",
        "left_keys": ["animal"],
        "right_keys": ["animal"],
        "how": "left",
    }
    for side in ("left", "right"):
        assert payload["inputs"][side]["backing_sha256"]
        assert payload["inputs"][side]["integrity"]["matches"] is True
    assert payload["join"]["cardinality"] == "many_to_one"
    assert payload["join"]["joined_rows"] == 10
    assert payload["join"]["left_rows_without_match"] == 0
    assert json.loads(json.dumps(payload, allow_nan=False)) == payload


def test_row_metric_over_the_replicated_side_is_warned(service):
    payload = service.aggregate_join(
        **_join(),
        metrics=[{"op": "n_distinct", "column": "right.animal"}],
    )
    assert any("repeated once per match" in item for item in payload["join"]["warnings"])


def test_unqualified_join_columns_are_refused(service):
    with pytest.raises(ValueError, match="must be qualified"):
        service.aggregate_join(**_join(), group_by=["genotype"], metrics=[{"op": "count"}])


def test_join_spec_is_either_named_or_explicit(service):
    with pytest.raises(ValueError, match="needs relationship_id"):
        service.aggregate_join(left="behaviour.csv", metrics=[{"op": "count"}])
    with pytest.raises(ValueError, match="not both"):
        service.aggregate_join(relationship_id="x", **_join(), metrics=[{"op": "count"}])


def test_declared_relationship_drives_aggregate_join(tmp_path: Path):
    result = ingest(_write_dataset(tmp_path), tmp_path / "out")
    builder = DatasetService(result.output_dir, load_relationships=False)
    bundle = builder.build_relationships(
        [
            {
                "left": "behaviour.csv",
                "right": "registry.csv",
                "left_keys": ["animal"],
                "right_keys": ["animal"],
                "expected_cardinality": "many_to_one",
            }
        ]
    )
    (result.output_dir / "relationships.json").write_text(json.dumps(bundle), encoding="utf-8")
    relation = next(item for item in bundle["relationships"] if item["status"] == "declared")

    payload = DatasetService(result.output_dir).aggregate_join(
        relationship_id=relation["id"],
        group_by=["right.genotype"],
        unit=["left.animal"],
        metrics=[{"op": "count"}],
    )
    assert payload["relationship_contract"]["id"] == relation["id"]
    assert payload["relationship_contract"]["status"] == "declared"
    assert _groups(payload, "right.genotype")[("KO",)]["n_units"] == 2


def test_many_to_many_join_is_refused(tmp_path: Path):
    registry = REGISTRY + "A,WT,vehicle\n"
    result = ingest(_write_dataset(tmp_path, registry=registry), tmp_path / "out")
    service = DatasetService(result.output_dir, load_relationships=False)
    with pytest.raises(ValueError, match="many-to-many"):
        service.aggregate_join(**_join(), metrics=[{"op": "count"}])


def test_inconsistent_unit_across_the_join_is_refused_with_locators(service):
    # A deliberately wrong unit: "treatment" spans both genotypes, so a unit
    # would have to be split across groups. The call must say so, not do it.
    payload = service.aggregate_join(
        **_join(),
        group_by=["right.genotype"],
        unit=["right.treatment"],
        metrics=[{"op": "count"}],
    )
    assert payload["analysis_unit"]["status"] == "refused"
    assert payload["groups"] == []
    conflict = payload["analysis_unit"]["inconsistent_units"]["units"][0]
    assert conflict["unit"] == {"right.treatment": "drug"}
    ko = conflict["observed_groups"][0]
    assert ko["group"] == {"right.genotype": "KO"}
    assert ko["source_rows"][0] == {"left": 2, "right": 2}


def test_join_scan_cap_refuses_rather_than_aggregating_a_partial_join(
    service, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(service_module, "_MAX_JOIN_SCAN_ROWS", 5)
    with pytest.raises(ValueError, match="safety cap is 5"):
        service.aggregate_join(**_join(), metrics=[{"op": "count"}])


def test_joined_row_cap_refuses_rather_than_truncating(service, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service_module, "_MAX_COMPLETE_QUERY_ROWS", 4)
    with pytest.raises(ValueError, match="complete-aggregation safety cap"):
        service.aggregate_join(**_join(), metrics=[{"op": "count"}])


def test_drifted_registry_withholds_the_join_aggregate(tmp_path: Path):
    source = _write_dataset(tmp_path)
    result = ingest(source, tmp_path / "out")
    (source / "registry.csv").write_text(REGISTRY.replace("C,WT", "C,KO"), encoding="utf-8")
    payload = DatasetService(result.output_dir, load_relationships=False).aggregate_join(
        **_join(), group_by=["right.genotype"], metrics=[{"op": "count"}]
    )
    assert payload["groups"] == []
    assert "right" in payload["content_withheld"]
    assert payload["inputs"]["right"]["integrity"]["matches"] is False
