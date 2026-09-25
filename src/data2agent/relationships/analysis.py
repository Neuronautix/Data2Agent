"""Evidence-bearing cross-table relationship assessment.

Relationship discovery is deliberately conservative. Matching column names and
overlapping values can suggest a join, but they do not prove that the dataset
authors intended one. Structural discovery therefore produces candidate
records only. Declared relationships require an explicit declaration (for
example, user configuration or a future supported metadata convention).

Every assessment records observed key completeness, uniqueness, cardinality,
overlap, backing-file checksums, and example source-row locators. No confidence
score is used as a substitute for epistemic status.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

from ..ingest.metadata import is_subject_identifier_name, normalise_column_name

RELATIONSHIP_VERSION = "0.1.0"
STATUSES = frozenset({"declared", "deterministic", "candidate", "rejected"})
CARDINALITIES = frozenset({"one_to_one", "one_to_many", "many_to_one", "many_to_many"})
_MAX_EVIDENCE_KEYS = 10


def candidate_key_specs(tables: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return conservative single-column structural join candidates.

    A shared normalised column name is considered only when it is a known
    subject-identifier name or at least one side established uniqueness during
    ingest. This avoids proposing joins merely because two measurement tables
    both happen to contain generic columns such as session or value.
    """
    paths = sorted(path for path, profile in tables.items() if profile.get("profiled", True))
    results: list[dict[str, Any]] = []
    for left_index, left_path in enumerate(paths):
        left_profile = tables[left_path]
        left_columns = _columns_by_normalised_name(left_profile)
        for right_path in paths[left_index + 1 :]:
            right_profile = tables[right_path]
            right_columns = _columns_by_normalised_name(right_profile)
            for normalised in sorted(set(left_columns) & set(right_columns)):
                left = left_columns[normalised]
                right = right_columns[normalised]
                if not (
                    is_subject_identifier_name(left["name"])
                    or left.get("unique") is True
                    or right.get("unique") is True
                ):
                    continue
                results.append(
                    {
                        "left": left_path,
                        "right": right_path,
                        "left_keys": [left["name"]],
                        "right_keys": [right["name"]],
                        "basis": {
                            "method": "shared-normalised-column",
                            "normalised_name": normalised,
                            "note": (
                                "structural candidate only: a shared column name and "
                                "compatible key shape do not establish author intent"
                            ),
                        },
                    }
                )
    return results


def assess_relationship(
    *,
    dataset_id: str,
    left_table: str,
    right_table: str,
    left_keys: list[str],
    right_keys: list[str],
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    left_context: dict[str, Any],
    right_context: dict[str, Any],
    status: str,
    basis: dict[str, Any],
    expected_cardinality: str | None = None,
) -> dict[str, Any]:
    """Assess one declared or candidate relationship from complete row scans."""
    if status not in STATUSES - {"rejected"}:
        raise ValueError(f"unsupported relationship status {status!r}")
    if not left_keys or len(left_keys) != len(right_keys):
        raise ValueError("left_keys and right_keys must be non-empty and have equal length")
    if expected_cardinality is not None and expected_cardinality not in CARDINALITIES:
        raise ValueError(
            f"unsupported expected cardinality {expected_cardinality!r}; "
            f"choose from {sorted(CARDINALITIES)}"
        )

    left = _key_facts(left_rows, left_keys)
    right = _key_facts(right_rows, right_keys)
    overlap = sorted(set(left["counts"]) & set(right["counts"]), key=_key_sort_key)
    cardinality = _cardinality(left["unique"], right["unique"])
    joined_rows = sum(left["counts"][key] * right["counts"][key] for key in overlap)

    final_status = status
    rejection_reasons: list[str] = []
    if status in {"declared", "deterministic"} and not overlap:
        final_status = "rejected"
        rejection_reasons.append("no complete key value occurs on both sides")
    if expected_cardinality is not None and cardinality != expected_cardinality:
        final_status = "rejected"
        rejection_reasons.append(
            f"observed cardinality is {cardinality}, expected {expected_cardinality}"
        )

    warnings: list[str] = []
    if cardinality == "many_to_many":
        warnings.append(
            "both sides contain duplicate complete keys; a join would multiply rows "
            "within matching keys"
        )
    if left["incomplete_rows"]:
        warnings.append(f"{left['incomplete_rows']} left row(s) have an incomplete key")
    if right["incomplete_rows"]:
        warnings.append(f"{right['incomplete_rows']} right row(s) have an incomplete key")

    relationship_id = stable_relationship_id(
        dataset_id, left_table, right_table, left_keys, right_keys
    )
    record: dict[str, Any] = {
        "id": relationship_id,
        "status": final_status,
        "left": _endpoint(left_table, left_keys, left_context, left, matched_distinct=len(overlap)),
        "right": _endpoint(
            right_table, right_keys, right_context, right, matched_distinct=len(overlap)
        ),
        "cardinality": cardinality,
        "matched_distinct_keys": len(overlap),
        "joined_row_count": joined_rows,
        "basis": basis,
        "evidence": {
            "kind": "observed-key-overlap",
            "examples": _evidence_examples(overlap, left["rows_by_key"], right["rows_by_key"]),
        },
        "warnings": warnings,
    }
    if expected_cardinality is not None:
        record["expected_cardinality"] = expected_cardinality
    if rejection_reasons:
        record["rejection_reasons"] = rejection_reasons
    return record


def stable_relationship_id(
    dataset_id: str,
    left_table: str,
    right_table: str,
    left_keys: list[str],
    right_keys: list[str],
) -> str:
    """Stable relation identity independent of candidate/declaration status."""
    payload = json.dumps(
        {
            "dataset_id": dataset_id,
            "left": left_table,
            "right": right_table,
            "left_keys": left_keys,
            "right_keys": right_keys,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "rel_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def declaration_key(
    declaration: dict[str, Any],
) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    """Canonical comparison key used to replace an equivalent candidate."""
    return (
        str(declaration["left"]),
        str(declaration["right"]),
        tuple(str(value) for value in declaration["left_keys"]),
        tuple(str(value) for value in declaration["right_keys"]),
    )


def _columns_by_normalised_name(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    collisions: set[str] = set()
    for column in profile.get("columns", []):
        name = normalise_column_name(str(column.get("name", "")))
        if not name:
            continue
        if name in by_name:
            collisions.add(name)
        else:
            by_name[name] = column
    for name in collisions:
        by_name.pop(name, None)
    return by_name


def _key_facts(rows: list[dict[str, Any]], keys: list[str]) -> dict[str, Any]:
    counts: Counter[tuple[Any, ...]] = Counter()
    rows_by_key: dict[tuple[Any, ...], list[int | None]] = defaultdict(list)
    incomplete = 0
    for row in rows:
        key = tuple(row["values"].get(column) for column in keys)
        if any(value is None for value in key):
            incomplete += 1
            continue
        counts[key] += 1
        rows_by_key[key].append(row.get("source_row"))
    complete = sum(counts.values())
    return {
        "rows": len(rows),
        "complete_rows": complete,
        "incomplete_rows": incomplete,
        "counts": counts,
        "rows_by_key": rows_by_key,
        "distinct_keys": len(counts),
        "unique": all(count == 1 for count in counts.values()),
    }


def _endpoint(
    table: str,
    keys: list[str],
    context: dict[str, Any],
    facts: dict[str, Any],
    *,
    matched_distinct: int,
) -> dict[str, Any]:
    distinct = facts["distinct_keys"]
    return {
        "table": table,
        "keys": list(keys),
        "backing_file": context["backing_file"],
        "backing_sha256": context["backing_sha256"],
        "rows": facts["rows"],
        "complete_key_rows": facts["complete_rows"],
        "incomplete_key_rows": facts["incomplete_rows"],
        "distinct_keys": distinct,
        "unique": facts["unique"],
        "matched_distinct_keys": matched_distinct,
        "distinct_key_coverage": (matched_distinct / distinct if distinct else 0.0),
    }


def _cardinality(left_unique: bool, right_unique: bool) -> str:
    if left_unique and right_unique:
        return "one_to_one"
    if left_unique:
        return "one_to_many"
    if right_unique:
        return "many_to_one"
    return "many_to_many"


def _evidence_examples(
    overlap: list[tuple[Any, ...]],
    left_rows: dict[tuple[Any, ...], list[int | None]],
    right_rows: dict[tuple[Any, ...], list[int | None]],
) -> list[dict[str, Any]]:
    return [
        {
            "key": list(key),
            "left_source_rows": left_rows[key][:_MAX_EVIDENCE_KEYS],
            "right_source_rows": right_rows[key][:_MAX_EVIDENCE_KEYS],
        }
        for key in overlap[:_MAX_EVIDENCE_KEYS]
    ]


def _key_sort_key(key: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(f"{type(value).__name__}:{value!s}" for value in key)
