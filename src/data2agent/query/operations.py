"""Deterministic query operations over evidence-bound table rows.

This module deliberately knows nothing about files, checksums, MCP, FAIR, or
semantics. It receives rows that the service has already read from verified
source bytes and applies a closed set of deterministic operations.

No expression language is accepted. Every filter/operator and aggregate is
enumerated here, so an agent cannot smuggle arbitrary code through a query.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

FILTER_OPERATORS = frozenset(
    {
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "in",
        "not_in",
        "contains",
        "is_missing",
        "is_not_missing",
    }
)
AGGREGATES = frozenset(
    {
        "count",
        "n_present",
        "n_missing",
        "n_distinct",
        "sum",
        "mean",
        "min",
        "max",
        "median",
        "sd",
        "sem",
    }
)
# The metrics that do arithmetic, and therefore refuse a column whose observed
# tokens were not all numbers. Counting metrics accept any column: how many
# values are present, absent or distinct is defined for strings too.
NUMERIC_AGGREGATES = frozenset({"sum", "mean", "min", "max", "median", "sd", "sem"})
# Every metric's definition, returned with every aggregation and mirrored in the
# docs, so that "sd" can never be read as the population deviation or "count"
# as a count of non-missing values. A metric without a written definition is
# not a metric.
METRIC_DEFINITIONS: dict[str, str] = {
    "count": "number of rows in the bucket (of units, at the unit stage), missing values included",
    "n_present": "number of non-missing values of the column",
    "n_missing": "number of missing values of the column (empty cell or convention sentinel)",
    "n_distinct": "number of distinct non-missing values of the column",
    "sum": "sum of the non-missing values",
    "mean": "arithmetic mean of the non-missing values",
    "min": "smallest non-missing value",
    "max": "largest non-missing value",
    "median": "middle non-missing value; the mean of the two middle values when n is even",
    "sd": (
        "sample standard deviation of the non-missing values, "
        "sqrt(sum((x - mean)^2) / (n - 1)); null when n < 2"
    ),
    "sem": "standard error of the mean, sd / sqrt(n), over the non-missing values; null when n < 2",
}
JOIN_TYPES = frozenset({"inner", "left"})


def filter_rows(
    rows: list[dict[str, Any]],
    filters: list[dict[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return the first matching rows and the total matches in the scanned rows."""
    _validate_filters(filters)
    matches: list[dict[str, Any]] = []
    total = 0
    for row in rows:
        values = row["values"]
        if all(_matches(values, rule) for rule in filters):
            total += 1
            if len(matches) < limit:
                matches.append(row)
    return matches, total


def aggregate_rows(
    rows: list[dict[str, Any]],
    *,
    group_by: list[str],
    metrics: list[dict[str, Any]],
    dtypes: dict[str, str],
) -> list[dict[str, Any]]:
    """Aggregate a complete row set by a closed metric registry."""
    specs = validate_metrics(metrics, dtypes)
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    if group_by:
        for row in rows:
            buckets[tuple(row["values"].get(name) for name in group_by)].append(row)
    else:
        buckets[()] = rows

    result: list[dict[str, Any]] = []
    for key in sorted(buckets, key=_group_sort_key):
        bucket = buckets[key]
        group = {name: key[index] for index, name in enumerate(group_by)}
        values, reasons = compute_metrics(bucket, specs)
        result.append({"group": group, "metrics": values, "null_reasons": reasons})
    return result


def compute_metrics(
    rows: list[dict[str, Any]], specs: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Apply validated metric specs to one bucket of rows.

    A null metric always comes with a reason. "No values" and "a statistic that
    is undefined for a single value" are different facts, and a bare null lets
    a reader take either one for the other -- or for a zero.
    """
    values: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    for spec in specs:
        value, reason = _aggregate_metric(rows, spec)
        values[spec["name"]] = value
        if reason is not None:
            reasons[spec["name"]] = reason
    return values, reasons


def describe_column(
    rows: list[dict[str, Any]],
    *,
    column: str,
    dtype: str,
) -> dict[str, Any]:
    """Compute a compact deterministic runtime description for one column."""
    values = [row["values"].get(column) for row in rows]
    present = [value for value in values if value is not None]
    payload: dict[str, Any] = {
        "count": len(rows),
        "non_missing": len(present),
        "n_missing": len(rows) - len(present),
    }
    if dtype in {"integer", "number"} and present:
        numeric = [_decimal(value, column) for value in present]
        total = sum(numeric, Decimal(0))
        payload.update(
            {
                "min": _json_number(min(numeric)),
                "max": _json_number(max(numeric)),
                "mean": float(total / Decimal(len(numeric))),
            }
        )
    return payload


def join_rows(
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    *,
    left_keys: list[str],
    right_keys: list[str],
    how: str,
    limit: int,
) -> dict[str, Any]:
    """Join complete bounded row sets on caller-declared keys.

    Null/missing key values never match, mirroring SQL NULL semantics rather
    than manufacturing a relationship between two absences.
    """
    if how not in JOIN_TYPES:
        raise ValueError(f"unsupported join type {how!r}; choose from {sorted(JOIN_TYPES)}")
    if not left_keys or len(left_keys) != len(right_keys):
        raise ValueError("left_keys and right_keys must be non-empty and have equal length")

    left_keyed, left_missing = _key_rows(left_rows, left_keys)
    right_keyed, right_missing = _key_rows(right_rows, right_keys)

    left_counts = Counter(key for key, _ in left_keyed)
    right_counts = Counter(key for key, _ in right_keyed)
    right_index: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for key, row in right_keyed:
        right_index[key].append(row)

    left_unique = all(count == 1 for count in left_counts.values())
    right_unique = all(count == 1 for count in right_counts.values())
    if left_unique and right_unique:
        cardinality = "one_to_one"
    elif left_unique:
        cardinality = "one_to_many"
    elif right_unique:
        cardinality = "many_to_one"
    else:
        cardinality = "many_to_many"

    total_result_rows = 0
    for key, count in left_counts.items():
        right_count = right_counts.get(key, 0)
        if right_count:
            total_result_rows += count * right_count
        elif how == "left":
            total_result_rows += count
    if how == "left":
        total_result_rows += len(left_missing)

    joined: list[dict[str, Any]] = []
    for key, left in left_keyed:
        matches = right_index.get(key)
        if matches:
            for right in matches:
                if len(joined) < limit:
                    joined.append(_joined_row(left, right))
        elif how == "left" and len(joined) < limit:
            joined.append(_joined_row(left, None))

    if how == "left":
        for left in left_missing:
            if len(joined) < limit:
                joined.append(_joined_row(left, None))

    diagnostics = {
        "cardinality": cardinality,
        "left_duplicate_keys": sum(1 for count in left_counts.values() if count > 1),
        "right_duplicate_keys": sum(1 for count in right_counts.values() if count > 1),
        "left_rows_with_missing_key": len(left_missing),
        "right_rows_with_missing_key": len(right_missing),
    }
    warnings: list[str] = []
    if cardinality == "many_to_many":
        warnings.append(
            "both sides contain duplicate join keys; matching rows are multiplied "
            "within each key (many-to-many join)"
        )

    return {
        "rows": joined,
        "returned": len(joined),
        "total_result_rows": total_result_rows,
        "truncated": total_result_rows > len(joined),
        "diagnostics": diagnostics,
        "warnings": warnings,
    }


def _validate_filters(filters: list[dict[str, Any]]) -> None:
    for index, rule in enumerate(filters):
        column = rule.get("column")
        op = rule.get("op")
        if not isinstance(column, str) or not column:
            raise ValueError(f"filter {index} needs a non-empty 'column'")
        if op not in FILTER_OPERATORS:
            raise ValueError(
                f"filter {index} has unsupported op {op!r}; choose from {sorted(FILTER_OPERATORS)}"
            )
        if op in {"in", "not_in"} and not isinstance(rule.get("value"), list):
            raise ValueError(f"filter {index} op {op!r} requires a list value")
        if op in {"is_missing", "is_not_missing"}:
            continue
        if "value" not in rule:
            raise ValueError(f"filter {index} op {op!r} requires 'value'")


def _matches(values: dict[str, Any], rule: dict[str, Any]) -> bool:
    column = rule["column"]
    if column not in values:
        raise KeyError(f"filter column {column!r} is not present in the selected row")
    actual = values[column]
    op = rule["op"]

    if op == "is_missing":
        return actual is None
    if op == "is_not_missing":
        return actual is not None

    expected = rule.get("value")
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "in":
        return actual in expected
    if op == "not_in":
        return actual not in expected
    if op == "contains":
        if actual is None:
            return False
        if not isinstance(actual, str) or not isinstance(expected, str):
            raise ValueError("'contains' requires string actual and expected values")
        return expected in actual

    if actual is None:
        return False
    _require_comparable(actual, expected, column)
    if op == "lt":
        return actual < expected
    if op == "lte":
        return actual <= expected
    if op == "gt":
        return actual > expected
    if op == "gte":
        return actual >= expected
    raise AssertionError(f"unreachable operator: {op}")


def _require_comparable(actual: Any, expected: Any, column: str) -> None:
    actual_numeric = _is_numeric(actual)
    expected_numeric = _is_numeric(expected)
    if actual_numeric and expected_numeric:
        return
    if isinstance(actual, str) and isinstance(expected, str):
        return
    raise ValueError(
        f"cannot order values for column {column!r}: "
        f"{type(actual).__name__} vs {type(expected).__name__}"
    )


def validate_metrics(
    metrics: list[dict[str, Any]], dtypes: dict[str, str], *, allow_empty: bool = False
) -> list[dict[str, Any]]:
    """Check metrics against the closed registry and resolve their output names."""
    if not isinstance(metrics, list) or (not metrics and not allow_empty):
        raise ValueError("at least one metric is required")

    specs: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, metric in enumerate(metrics):
        if not isinstance(metric, dict):
            raise ValueError(f"metric {index} must be an object")
        op = metric.get("op")
        column = metric.get("column")
        if op not in AGGREGATES:
            raise ValueError(
                f"metric {index} has unsupported op {op!r}; choose from {sorted(AGGREGATES)}"
            )
        if op != "count":
            if not isinstance(column, str) or column not in dtypes:
                raise ValueError(f"metric {index} op {op!r} requires a known column")
        elif column is not None:
            raise ValueError("'count' counts rows and does not accept a column")

        if op in NUMERIC_AGGREGATES and dtypes[column] not in {"integer", "number"}:
            raise ValueError(
                f"metric {op!r} requires a numeric column; {column!r} has dtype {dtypes[column]!r}"
            )

        default_name = op if column is None else f"{op}:{column}"
        name = metric.get("name") or default_name
        if not isinstance(name, str) or not name:
            raise ValueError(f"metric {index} has an invalid output name")
        if name in names:
            raise ValueError(f"duplicate metric output name {name!r}")
        names.add(name)
        specs.append({"op": op, "column": column, "name": name})
    return specs


def _aggregate_metric(rows: list[dict[str, Any]], spec: dict[str, Any]) -> tuple[Any, str | None]:
    op = spec["op"]
    column = spec["column"]
    if op == "count":
        return len(rows), None

    observed = [row["values"].get(column) for row in rows]
    present = [value for value in observed if value is not None]
    if op == "n_missing":
        return len(observed) - len(present), None
    if op == "n_present":
        return len(present), None
    if op == "n_distinct":
        # Type-qualified, so the string "1" and the number 1 stay distinct.
        return len({(type(value).__name__, value) for value in present}), None

    if not present:
        return None, "no non-missing values"
    numeric = [_decimal(value, column) for value in present]
    if op == "sum":
        return _json_number(sum(numeric, Decimal(0))), None
    if op == "mean":
        return float(sum(numeric, Decimal(0)) / Decimal(len(numeric))), None
    if op == "min":
        return _json_number(min(numeric)), None
    if op == "max":
        return _json_number(max(numeric)), None
    if op == "median":
        return _json_number(statistics.median(numeric)), None
    if op in {"sd", "sem"}:
        n = len(numeric)
        if n < 2:
            # The sample deviation divides by n - 1. Returning 0 for one value
            # would claim a measured absence of spread; there is no measurement.
            return None, f"{op} is undefined for fewer than 2 non-missing values (n={n})"
        # Decimal throughout, with headroom over the default 28 digits, so the
        # squared deviations do not lose the digits that distinguish them. The
        # result is rounded exactly once, to float, on the way out.
        with localcontext() as context:
            context.prec = 50
            mean = sum(numeric, Decimal(0)) / Decimal(n)
            squares = sum(((value - mean) ** 2 for value in numeric), Decimal(0))
            sd = (squares / Decimal(n - 1)).sqrt()
            result = sd if op == "sd" else sd / Decimal(n).sqrt()
        return float(result), None
    raise AssertionError(f"unreachable aggregate: {op}")


def _decimal(value: Any, column: str) -> Decimal:
    if not _is_numeric(value):
        raise ValueError(f"non-numeric value {value!r} encountered in numeric column {column!r}")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"cannot aggregate value {value!r} in column {column!r}") from exc


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _group_sort_key(key: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(f"{type(value).__name__}:{value!s}" for value in key)


def _key_rows(
    rows: list[dict[str, Any]], keys: list[str]
) -> tuple[list[tuple[tuple[Any, ...], dict[str, Any]]], list[dict[str, Any]]]:
    keyed: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    missing: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(row["values"].get(name) for name in keys)
        if any(value is None for value in key):
            missing.append(row)
        else:
            keyed.append((key, row))
    return keyed, missing


def _joined_row(left: dict[str, Any], right: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "source_rows": {
            "left": left.get("source_row"),
            "right": right.get("source_row") if right else None,
        },
        "left": left["values"],
        "right": right["values"] if right else None,
    }
