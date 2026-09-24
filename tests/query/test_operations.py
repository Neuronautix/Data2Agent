"""Deterministic query operations over already-evidence-bound rows."""

from __future__ import annotations

import pytest

from data2agent.query import aggregate_rows, filter_rows, join_rows


def _row(source_row, **values):
    return {"source_row": source_row, "values": values, "missing": {}}


def test_filter_registry_rejects_free_form_operators():
    rows = [_row(2, group="A")]
    with pytest.raises(ValueError, match="unsupported op"):
        filter_rows(
            rows,
            [{"column": "group", "op": "__import__", "value": "os"}],
            limit=10,
        )


def test_many_to_many_join_is_explicitly_diagnosed():
    left = [
        _row(2, id="A", left_value=1),
        _row(3, id="A", left_value=2),
    ]
    right = [
        _row(10, id="A", right_value=3),
        _row(11, id="A", right_value=4),
    ]

    result = join_rows(
        left,
        right,
        left_keys=["id"],
        right_keys=["id"],
        how="inner",
        limit=10,
    )

    assert result["diagnostics"]["cardinality"] == "many_to_many"
    assert result["total_result_rows"] == 4
    assert result["returned"] == 4
    assert result["warnings"]


def test_missing_join_keys_never_match_each_other():
    left = [_row(2, id=None, value="left")]
    right = [_row(10, id=None, value="right")]

    result = join_rows(
        left,
        right,
        left_keys=["id"],
        right_keys=["id"],
        how="inner",
        limit=10,
    )

    assert result["total_result_rows"] == 0
    assert result["rows"] == []
    assert result["diagnostics"]["left_rows_with_missing_key"] == 1
    assert result["diagnostics"]["right_rows_with_missing_key"] == 1


def test_numeric_aggregate_rejects_string_dtype():
    rows = [_row(2, group="A")]
    with pytest.raises(ValueError, match="requires a numeric column"):
        aggregate_rows(
            rows,
            group_by=[],
            metrics=[{"op": "mean", "column": "group"}],
            dtypes={"group": "string"},
        )
