"""Two-stage aggregation over a declared unit of analysis.

Scientific tables are usually long: one row per time bin, trial or repeat, many
rows per animal. Averaging such rows by group answers a question nobody asked.
An animal with twelve bins weighs twelve times as much as one with a single
bin, and the "n" beside the mean counts rows, not animals. The number looks
exactly like the right one, which is what makes it dangerous.

So the caller declares the experimental unit (``unit=["animal", "day"]``), and
aggregation runs in two explicit stages:

1. **Unit stage** -- rows are reduced to one record per unit with the
   ``unit_metrics`` (for example the sum of a duration over time bins);
2. **Group stage** -- unit records are summarised per group with ``metrics``,
   whose columns name unit-stage outputs, so ``count`` there counts units.

Nothing about the unit is inferred. Which columns identify it and how rows
reduce to it are scientific decisions that the caller states and the response
repeats back.

A unit is also a claim about the grouping: one animal has one genotype. When the
rows of a unit disagree on a group_by value, the data contradict the declared
design, and neither splitting the unit across groups nor merging it into one is
defensible. That contradiction is reported with source-row locators and, by
default, the whole result is refused; ``on_inconsistent_unit="exclude"``
computes over the consistent units while listing every excluded one.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .operations import _group_sort_key, compute_metrics, validate_metrics

INCONSISTENT_UNIT_POLICIES = frozenset({"refuse", "exclude"})

# Bounds on the audit trail. Counts are always exact; only the listed examples
# are capped, and every capped list says so.
MAX_UNIT_SAMPLE = 500
_SOURCE_ROWS_PER_UNIT = 20
_INCONSISTENT_UNITS_LISTED = 100
_MISSING_UNIT_ROWS_LISTED = 20
_UNIT_METRIC_DTYPE = "number"


def aggregate_units(
    rows: list[dict[str, Any]],
    *,
    group_by: list[str],
    unit: list[str],
    unit_metrics: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    dtypes: dict[str, str],
    on_inconsistent_unit: str = "refuse",
    unit_sample: int = 50,
) -> dict[str, Any]:
    """Reduce rows to units, then summarise units per group."""
    if not isinstance(unit, list) or not unit:
        raise ValueError("unit must be a non-empty list of column names")
    unknown = [name for name in unit if name not in dtypes]
    if unknown:
        raise ValueError(f"unknown unit column(s): {unknown}")
    if len(set(unit)) != len(unit):
        raise ValueError("unit columns must not repeat")
    if on_inconsistent_unit not in INCONSISTENT_UNIT_POLICIES:
        raise ValueError(
            f"unsupported on_inconsistent_unit {on_inconsistent_unit!r}; "
            f"choose from {sorted(INCONSISTENT_UNIT_POLICIES)}"
        )
    sample = int(unit_sample)
    if sample < 0:
        raise ValueError("unit_sample must be zero or greater")
    sample = min(sample, MAX_UNIT_SAMPLE)

    unit_specs = validate_metrics(unit_metrics, dtypes, allow_empty=True)
    # Unit-stage outputs are all numbers (counts or arithmetic), so they form the
    # only columns the group stage may read. A group-stage metric naming a raw
    # table column is refused: it would silently skip the unit stage.
    stage_two_dtypes = {spec["name"]: _UNIT_METRIC_DTYPE for spec in unit_specs}
    try:
        group_specs = validate_metrics(metrics, stage_two_dtypes)
    except ValueError as error:
        raise ValueError(
            f"{error}. With a unit declared, metrics summarise units and their columns "
            f"must name unit_metrics outputs: {sorted(stage_two_dtypes)}"
        ) from error

    # -- stage 0: assign rows to units ------------------------------------
    by_unit: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    missing_unit_rows: list[Any] = []
    missing_unit_count = 0
    for row in rows:
        key = tuple(row["values"].get(name) for name in unit)
        if any(value is None for value in key):
            # A row that names no unit cannot be attributed to one. Guessing
            # (for example, carrying the previous row's animal forward) would be
            # an inference, so the row is counted and located instead.
            missing_unit_count += 1
            if len(missing_unit_rows) < _MISSING_UNIT_ROWS_LISTED:
                missing_unit_rows.append(row.get("source_row"))
            continue
        by_unit[key].append(row)

    # -- consistency: one unit, one group ---------------------------------
    unit_group: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    inconsistent: list[dict[str, Any]] = []
    for key in sorted(by_unit, key=_group_sort_key):
        observed: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in by_unit[key]:
            observed[tuple(row["values"].get(name) for name in group_by)].append(row)
        if len(observed) == 1:
            unit_group[key] = next(iter(observed))
            continue
        inconsistent.append(
            {
                "unit": _named(unit, key),
                "n_rows": len(by_unit[key]),
                "observed_groups": [
                    {
                        "group": _named(group_by, group_key),
                        "n_rows": len(observed[group_key]),
                        "source_rows": [
                            row.get("source_row")
                            for row in observed[group_key][:_SOURCE_ROWS_PER_UNIT]
                        ],
                    }
                    for group_key in sorted(observed, key=_group_sort_key)
                ],
            }
        )

    result: dict[str, Any] = {
        "unit": list(unit),
        "on_inconsistent_unit": on_inconsistent_unit,
        "rows_with_missing_unit_key": {
            "count": missing_unit_count,
            "source_rows": missing_unit_rows,
            "truncated": missing_unit_count > len(missing_unit_rows),
        },
        "inconsistent_units": {
            "count": len(inconsistent),
            "units": inconsistent[:_INCONSISTENT_UNITS_LISTED],
            "truncated": len(inconsistent) > _INCONSISTENT_UNITS_LISTED,
        },
    }
    if inconsistent and on_inconsistent_unit == "refuse":
        result.update(
            {
                "status": "refused",
                "reason": (
                    f"{len(inconsistent)} unit(s) carry more than one combination of the "
                    f"group_by columns {group_by}; a unit cannot be split across groups or "
                    "merged into one without a decision the data do not record. Fix the "
                    "unit/group_by declaration, or pass on_inconsistent_unit='exclude' to "
                    "summarise only the consistent units."
                ),
                "n_units": len(unit_group),
                "groups": [],
            }
        )
        return result

    # -- stage 1: one record per unit --------------------------------------
    records: dict[tuple[Any, ...], dict[str, Any]] = {}
    for key in unit_group:
        unit_rows = by_unit[key]
        values, reasons = compute_metrics(unit_rows, unit_specs)
        records[key] = {
            "unit": _named(unit, key),
            "n_rows": len(unit_rows),
            "values": values,
            "null_reasons": reasons,
            "missing": _missing_profile(unit_rows, unit_specs),
            "source_rows": [row.get("source_row") for row in unit_rows[:_SOURCE_ROWS_PER_UNIT]],
            "source_rows_truncated": len(unit_rows) > _SOURCE_ROWS_PER_UNIT,
        }

    # -- stage 2: summarise units per group --------------------------------
    by_group: dict[tuple[Any, ...], list[tuple[Any, ...]]] = defaultdict(list)
    for key in sorted(records, key=_group_sort_key):
        by_group[unit_group[key]].append(key)

    groups: list[dict[str, Any]] = []
    for group_key in sorted(by_group, key=_group_sort_key):
        members = by_group[group_key]
        unit_rows = [{"values": records[key]["values"]} for key in members]
        values, reasons = compute_metrics(unit_rows, group_specs)
        groups.append(
            {
                "group": _named(group_by, group_key),
                "n_units": len(members),
                "n_rows": sum(records[key]["n_rows"] for key in members),
                "metrics": values,
                "null_reasons": reasons,
                "unit_metric_missing": {
                    spec["name"]: {
                        "units_null": sum(
                            records[key]["values"][spec["name"]] is None for key in members
                        ),
                        "units_with_some_missing_rows": sum(
                            records[key]["missing"].get(spec["name"]) == "partial"
                            for key in members
                        ),
                    }
                    for spec in unit_specs
                    if spec["column"] is not None
                },
                "units": [_public(records[key]) for key in members[:sample]],
                "units_listed": min(len(members), sample),
                "units_truncated": len(members) > sample,
            }
        )

    result.update(
        {
            "status": "computed",
            "n_units": len(records),
            "excluded_inconsistent_units": len(inconsistent),
            "groups": groups,
        }
    )
    return result


def _missing_profile(rows: list[dict[str, Any]], specs: list[dict[str, Any]]) -> dict[str, str]:
    """Classify each column-bound unit metric's inputs as complete, partial or empty.

    A partial unit matters most: a sum over the bins that happen to be present
    is smaller than the sum over all bins, and nothing in the number says so.
    """
    profile: dict[str, str] = {}
    for spec in specs:
        column = spec["column"]
        if column is None:
            continue
        missing = sum(row["values"].get(column) is None for row in rows)
        if missing == 0:
            profile[spec["name"]] = "complete"
        elif missing == len(rows):
            profile[spec["name"]] = "all_missing"
        else:
            profile[spec["name"]] = "partial"
    return profile


def _public(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "unit": record["unit"],
        "n_rows": record["n_rows"],
        "values": record["values"],
        **({"null_reasons": record["null_reasons"]} if record["null_reasons"] else {}),
        **(
            {"rows_missing": {k: v for k, v in record["missing"].items() if v != "complete"}}
            if any(v != "complete" for v in record["missing"].values())
            else {}
        ),
        "source_rows": record["source_rows"],
        "source_rows_truncated": record["source_rows_truncated"],
    }


def _named(names: list[str], key: tuple[Any, ...]) -> dict[str, Any]:
    return {name: key[index] for index, name in enumerate(names)}
