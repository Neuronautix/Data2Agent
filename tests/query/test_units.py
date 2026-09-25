"""Unit-of-analysis aggregation and the dispersion metrics, on in-code rows."""

from __future__ import annotations

import json
import math
import statistics

import pytest

from data2agent.query import AGGREGATES, METRIC_DEFINITIONS, aggregate_rows, aggregate_units

DTYPES = {"animal": "string", "day": "integer", "genotype": "string", "dur": "number"}


def _row(source_row, **values):
    return {"source_row": source_row, "values": values, "missing": {}}


def _bins():
    """Unequal repeated measures: A has three bins, B one, C one of two missing."""
    return [
        _row(2, animal="A", day=1, genotype="KO", dur=10.0),
        _row(3, animal="A", day=1, genotype="KO", dur=20.0),
        _row(4, animal="A", day=1, genotype="KO", dur=30.0),
        _row(5, animal="B", day=1, genotype="KO", dur=5.0),
        _row(6, animal="C", day=1, genotype="WT", dur=1.0),
        _row(7, animal="C", day=1, genotype="WT", dur=None),
        _row(8, animal="D", day=1, genotype="WT", dur=None),
    ]


def _by_group(result, name="genotype"):
    return {item["group"][name]: item for item in result["groups"]}


def units_of(result, genotype):
    return {unit["unit"]["animal"]: unit for unit in _by_group(result)[genotype]["units"]}


def test_unit_mean_differs_from_row_mean_and_n_counts_units():
    rows = [row for row in _bins() if row["values"]["genotype"] == "KO"]
    naive = aggregate_rows(
        rows,
        group_by=["genotype"],
        metrics=[{"op": "mean", "column": "dur"}, {"op": "count"}],
        dtypes=DTYPES,
    )[0]["metrics"]
    two_stage = aggregate_units(
        rows,
        group_by=["genotype"],
        unit=["animal", "day"],
        unit_metrics=[{"op": "sum", "column": "dur"}],
        metrics=[{"op": "mean", "column": "sum:dur"}, {"op": "count"}],
        dtypes=DTYPES,
    )
    ko = _by_group(two_stage)["KO"]

    # The row mean weights A's three bins three times; the unit mean does not.
    assert naive["mean:dur"] == pytest.approx(16.25)
    assert naive["count"] == 4
    assert ko["metrics"]["mean:sum:dur"] == pytest.approx(32.5)
    assert ko["metrics"]["count"] == 2
    assert ko["metrics"]["mean:sum:dur"] != pytest.approx(naive["mean:dur"])
    assert (ko["n_units"], ko["n_rows"]) == (2, 4)
    assert two_stage["status"] == "computed"


def test_every_group_lists_its_contributing_units_with_row_locators():
    result = aggregate_units(
        _bins(),
        group_by=["genotype"],
        unit=["animal", "day"],
        unit_metrics=[{"op": "sum", "column": "dur"}, {"op": "count", "name": "bins"}],
        metrics=[{"op": "count"}],
        dtypes=DTYPES,
    )
    ko = _by_group(result)["KO"]
    assert [unit["unit"] for unit in ko["units"]] == [
        {"animal": "A", "day": 1},
        {"animal": "B", "day": 1},
    ]
    assert ko["units"][0]["source_rows"] == [2, 3, 4]
    assert ko["units"][0]["values"] == {"sum:dur": 60, "bins": 3}
    assert ko["units_listed"] == 2 and ko["units_truncated"] is False


def test_unit_sample_bounds_the_listing_but_never_the_counts():
    result = aggregate_units(
        _bins(),
        group_by=["genotype"],
        unit=["animal"],
        unit_metrics=[],
        metrics=[{"op": "count"}],
        dtypes=DTYPES,
        unit_sample=1,
    )
    ko = _by_group(result)["KO"]
    assert ko["n_units"] == 2 and ko["metrics"]["count"] == 2
    assert ko["units_listed"] == 1 and ko["units_truncated"] is True


def test_a_unit_under_two_groups_refuses_the_result_by_default():
    rows = [*_bins(), _row(9, animal="A", day=1, genotype="WT", dur=4.0)]
    result = aggregate_units(
        rows,
        group_by=["genotype"],
        unit=["animal", "day"],
        unit_metrics=[{"op": "sum", "column": "dur"}],
        metrics=[{"op": "mean", "column": "sum:dur"}],
        dtypes=DTYPES,
    )
    assert result["status"] == "refused"
    assert result["groups"] == []
    conflict = result["inconsistent_units"]["units"][0]
    assert conflict["unit"] == {"animal": "A", "day": 1}
    assert [item["group"] for item in conflict["observed_groups"]] == [
        {"genotype": "KO"},
        {"genotype": "WT"},
    ]
    assert conflict["observed_groups"][1]["source_rows"] == [9]


def test_excluding_inconsistent_units_is_explicit_and_never_splits_them():
    rows = [*_bins(), _row(9, animal="A", day=1, genotype="WT", dur=4.0)]
    result = aggregate_units(
        rows,
        group_by=["genotype"],
        unit=["animal", "day"],
        unit_metrics=[{"op": "sum", "column": "dur"}],
        metrics=[{"op": "count"}],
        dtypes=DTYPES,
        on_inconsistent_unit="exclude",
    )
    assert result["status"] == "computed"
    assert result["excluded_inconsistent_units"] == 1
    listed = [unit["unit"]["animal"] for group in result["groups"] for unit in group["units"]]
    assert "A" not in listed
    assert _by_group(result)["KO"]["n_units"] == 1


def test_a_missing_group_value_inside_a_unit_is_a_conflict_not_a_merge():
    rows = [
        _row(2, animal="A", day=1, genotype="KO", dur=1.0),
        _row(3, animal="A", day=1, genotype=None, dur=2.0),
    ]
    result = aggregate_units(
        rows,
        group_by=["genotype"],
        unit=["animal", "day"],
        unit_metrics=[],
        metrics=[{"op": "count"}],
        dtypes=DTYPES,
    )
    assert result["status"] == "refused"


def test_missing_values_are_excluded_per_metric_and_reported_per_unit():
    result = aggregate_units(
        _bins(),
        group_by=["genotype"],
        unit=["animal", "day"],
        unit_metrics=[{"op": "sum", "column": "dur"}, {"op": "n_missing", "column": "dur"}],
        metrics=[
            {"op": "mean", "column": "sum:dur"},
            {"op": "n_missing", "column": "sum:dur"},
            {"op": "count"},
        ],
        dtypes=DTYPES,
    )
    wt = _by_group(result)["WT"]
    # C keeps its one present bin; D has none, so its unit value is null.
    assert wt["metrics"] == {"mean:sum:dur": 1, "n_missing:sum:dur": 1, "count": 2}
    assert wt["unit_metric_missing"]["sum:dur"] == {
        "units_null": 1,
        "units_with_some_missing_rows": 1,
    }
    units = {unit["unit"]["animal"]: unit for unit in wt["units"]}
    assert units["C"]["rows_missing"]["sum:dur"] == "partial"
    assert units["C"]["values"]["n_missing:dur"] == 1
    assert "rows_missing" not in units_of(result, "KO")["A"]
    assert units["D"]["values"]["sum:dur"] is None
    assert units["D"]["null_reasons"]["sum:dur"] == "no non-missing values"


def test_rows_without_a_unit_key_are_counted_and_located_not_attributed():
    rows = [*_bins(), _row(9, animal=None, day=1, genotype="KO", dur=99.0)]
    result = aggregate_units(
        rows,
        group_by=["genotype"],
        unit=["animal", "day"],
        unit_metrics=[{"op": "sum", "column": "dur"}],
        metrics=[{"op": "count"}],
        dtypes=DTYPES,
    )
    assert result["rows_with_missing_unit_key"] == {
        "count": 1,
        "source_rows": [9],
        "truncated": False,
    }
    assert _by_group(result)["KO"]["n_rows"] == 4


def test_group_stage_metrics_must_read_unit_outputs_not_raw_columns():
    with pytest.raises(ValueError, match="must name unit_metrics outputs"):
        aggregate_units(
            _bins(),
            group_by=["genotype"],
            unit=["animal"],
            unit_metrics=[{"op": "sum", "column": "dur"}],
            metrics=[{"op": "mean", "column": "dur"}],
            dtypes=DTYPES,
        )


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"unit": []}, "non-empty"),
        ({"unit": ["cage"]}, "unknown unit column"),
        ({"unit": ["animal"], "on_inconsistent_unit": "merge"}, "on_inconsistent_unit"),
    ],
)
def test_unit_declaration_is_validated(kwargs, message):
    with pytest.raises(ValueError, match=message):
        aggregate_units(
            _bins(),
            group_by=["genotype"],
            unit_metrics=[],
            metrics=[{"op": "count"}],
            dtypes=DTYPES,
            **kwargs,
        )


def _values(*numbers):
    return [_row(index + 2, x=value) for index, value in enumerate(numbers)]


def _metric(rows, op):
    result = aggregate_rows(
        rows, group_by=[], metrics=[{"op": op, "column": "x"}], dtypes={"x": "number"}
    )[0]
    return result["metrics"][f"{op}:x"], result["null_reasons"].get(f"{op}:x")


def test_sd_is_the_sample_deviation_and_sem_divides_by_root_n():
    data = [2, 4, 4, 4, 5, 5, 7, 9]
    rows = _values(*data)
    sd, _ = _metric(rows, "sd")
    sem, _ = _metric(rows, "sem")
    assert sd == pytest.approx(statistics.stdev(data))
    assert sd == pytest.approx(math.sqrt(32 / 7))
    assert sd != pytest.approx(statistics.pstdev(data))
    assert sem == pytest.approx(statistics.stdev(data) / math.sqrt(8))


def test_median_handles_odd_and_even_counts():
    assert _metric(_values(3, 1, 2), "median") == (2, None)
    assert _metric(_values(4, 1, 3, 2), "median") == (2.5, None)


@pytest.mark.parametrize("op", ["sd", "sem"])
def test_dispersion_is_null_with_a_reason_below_two_values(op):
    value, reason = _metric(_values(5, None), op)
    assert value is None
    assert "fewer than 2" in reason and "n=1" in reason


def test_counting_metrics_distinguish_present_missing_and_distinct():
    rows = _values(1, 1, None, 2)
    assert _metric(rows, "n_present") == (3, None)
    assert _metric(rows, "n_missing") == (1, None)
    assert _metric(rows, "n_distinct") == (2, None)
    assert _metric(_values(None, None), "mean") == (None, "no non-missing values")


def test_every_registered_metric_has_a_written_definition():
    assert set(METRIC_DEFINITIONS) == set(AGGREGATES)


def test_unit_results_are_strict_json():
    result = aggregate_units(
        _bins(),
        group_by=["genotype"],
        unit=["animal", "day"],
        unit_metrics=[{"op": "sum", "column": "dur"}],
        metrics=[
            {"op": "mean", "column": "sum:dur"},
            {"op": "sd", "column": "sum:dur"},
            {"op": "sem", "column": "sum:dur"},
            {"op": "median", "column": "sum:dur"},
        ],
        dtypes=DTYPES,
    )
    assert json.loads(json.dumps(result, allow_nan=False)) == result
