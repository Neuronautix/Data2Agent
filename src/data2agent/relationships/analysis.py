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
from .crosswalk import KeyResolver, key_label, mapping_facts

# 0.2.0: optional declared key mapping (crosswalk and/or key_format) per record
# and the bundle-level ``crosswalks`` registry. Bundles without them read as 0.1.0.
RELATIONSHIP_VERSION = "0.2.0"
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
    left_resolver: KeyResolver | None = None,
    right_resolver: KeyResolver | None = None,
) -> dict[str, Any]:
    """Assess one declared or candidate relationship from complete row scans.

    With resolvers (a declared crosswalk and/or key_format), every fact --
    overlap, uniqueness, cardinality, completeness -- is computed on the
    resolved key, i.e. on canonical IDs, because that is what a join through the
    same declaration would compare. What the mapping did is recorded beside it
    in ``key_mapping`` so the canonical-level facts are never detached from the
    raw values they came from.
    """
    if status not in STATUSES - {"rejected"}:
        raise ValueError(f"unsupported relationship status {status!r}")
    if not left_keys or not right_keys:
        raise ValueError("left_keys and right_keys must be non-empty and have equal length")
    if expected_cardinality is not None and expected_cardinality not in CARDINALITIES:
        raise ValueError(
            f"unsupported expected cardinality {expected_cardinality!r}; "
            f"choose from {sorted(CARDINALITIES)}"
        )

    transformed = any(
        resolver is not None and resolver.transforms for resolver in (left_resolver, right_resolver)
    )
    if transformed:
        left_resolver = left_resolver or KeyResolver(list(left_keys), side="left", as_text=True)
        right_resolver = right_resolver or KeyResolver(list(right_keys), side="right", as_text=True)
        if (left_resolver.crosswalk is None) != (right_resolver.crosswalk is None):
            raise ValueError("a crosswalk must apply to both sides of a relationship")
        if left_resolver.width != right_resolver.width:
            raise ValueError(
                "after key_format rendering, left and right keys must have the same width"
            )
    elif len(left_keys) != len(right_keys):
        raise ValueError("left_keys and right_keys must be non-empty and have equal length")
    left = _key_facts(left_rows, left_keys, left_resolver if transformed else None)
    right = _key_facts(right_rows, right_keys, right_resolver if transformed else None)
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

    key_mapping: dict[str, Any] | None = None
    if transformed:
        assert left_resolver is not None and right_resolver is not None
        key_mapping = {
            "crosswalk": (
                left_resolver.crosswalk.citation() if left_resolver.crosswalk is not None else None
            ),
            "left": mapping_facts(left_rows, left_resolver, other_keys=set(right["counts"])),
            "right": mapping_facts(right_rows, right_resolver, other_keys=set(left["counts"])),
            "note": (
                "overlap, uniqueness and cardinality were computed on the resolved key; "
                "a value absent from the crosswalk was passed through unchanged, kept apart "
                "from canonical IDs, and never guessed"
                if left_resolver.crosswalk is not None
                else "overlap, uniqueness and cardinality were computed on the key as rendered "
                "by the declared key_format; no crosswalk was applied"
            ),
        }
        for side in ("left", "right"):
            rendered = key_mapping[side].get("rendering_collisions") or []
            if rendered and status in {"declared", "deterministic"}:
                final_status = "rejected"
                rejection_reasons.append(
                    f"rendering collision on the {side} side: {len(rendered)} rendered key(s) "
                    "are produced by more than one distinct raw key, so the rendered key "
                    "cannot tell those subjects apart"
                )
            collisions = key_mapping[side].get("collisions") or []
            if collisions and status in {"declared", "deterministic"}:
                final_status = "rejected"
                rejection_reasons.append(
                    f"crosswalk collision on the {side} side: {len(collisions)} canonical ID(s) "
                    "are written in more than one form within the same table; joining through "
                    "the crosswalk would merge them, which only the data owner can confirm"
                )

    warnings: list[str] = []
    if key_mapping is not None:
        for side, resolver in (("left", left_resolver), ("right", right_resolver)):
            facts = key_mapping[side]
            if resolver.key_format is not None and resolver.key_format.adjacent_placeholders:
                warnings.append(
                    f"{side}_key_format {resolver.key_format.template!r} places key columns "
                    "side by side with no separator, so distinct raw keys can render alike"
                )
            if facts.get("rendering_collisions"):
                warnings.append(
                    f"{side}: {len(facts['rendering_collisions'])} rendered key(s) come from "
                    "more than one distinct raw key (reported, not merged)"
                )
            if facts.get("collisions"):
                warnings.append(
                    f"{side}: {len(facts['collisions'])} canonical ID(s) collect more than one "
                    "distinct written form in this table (reported, not merged)"
                )
            if facts.get("unmapped_rows"):
                warnings.append(
                    f"{side}: {facts['unmapped_rows']} row(s) carry "
                    f"{facts['unmapped_distinct_values']} key value(s) absent from the crosswalk; "
                    "they were passed through unchanged and match only identical unmapped values"
                )
            if facts.get("unmapped_values_equal_to_a_canonical_id"):
                warnings.append(
                    f"{side}: unmapped value(s) equal a canonical ID but are not listed as a "
                    "form, so they do not join that canonical ID; list them as forms if they "
                    "denote it"
                )
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
        dataset_id,
        left_table,
        right_table,
        left_keys,
        right_keys,
        mapping=_mapping_identity(left_resolver, right_resolver) if transformed else None,
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
            "examples": _evidence_examples(
                overlap,
                left["rows_by_key"],
                right["rows_by_key"],
                resolver=left_resolver if transformed else None,
            ),
        },
        "warnings": warnings,
    }
    if key_mapping is not None:
        record["key_mapping"] = key_mapping
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
    mapping: dict[str, Any] | None = None,
) -> str:
    """Stable relation identity independent of candidate/declaration status.

    A declared key mapping is part of the identity: the same columns joined by
    exact equality and joined through a crosswalk are different relationships
    with different facts. The crosswalk is identified by name, not hash, so a
    corrected crosswalk keeps the relationship id while its facts are re-derived.
    Without a mapping the payload is unchanged, so existing ids are stable.
    """
    identity: dict[str, Any] = {
        "dataset_id": dataset_id,
        "left": left_table,
        "right": right_table,
        "left_keys": left_keys,
        "right_keys": right_keys,
    }
    if mapping:
        identity["key_mapping"] = mapping
    payload = json.dumps(
        identity,
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


def _mapping_identity(left: KeyResolver | None, right: KeyResolver | None) -> dict[str, Any] | None:
    identity: dict[str, Any] = {}
    for side, resolver in (("left", left), ("right", right)):
        if resolver is not None and resolver.key_format is not None:
            identity[f"{side}_key_format"] = resolver.key_format.template
    crosswalk = (left.crosswalk if left is not None else None) or (
        right.crosswalk if right is not None else None
    )
    if crosswalk is not None:
        identity["crosswalk"] = crosswalk.name
    return identity or None


def _key_facts(
    rows: list[dict[str, Any]], keys: list[str], resolver: KeyResolver | None = None
) -> dict[str, Any]:
    counts: Counter[tuple[Any, ...]] = Counter()
    rows_by_key: dict[tuple[Any, ...], list[int | None]] = defaultdict(list)
    incomplete = 0
    for row in rows:
        if resolver is not None:
            key = resolver.resolve(row["values"])
        else:
            key = tuple(row["values"].get(column) for column in keys)
            if any(value is None for value in key):
                key = None
        if key is None:
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
    *,
    resolver: KeyResolver | None = None,
) -> list[dict[str, Any]]:
    examples = []
    for key in overlap[:_MAX_EVIDENCE_KEYS]:
        example: dict[str, Any] = {
            "key": list(key),
            "left_source_rows": left_rows[key][:_MAX_EVIDENCE_KEYS],
            "right_source_rows": right_rows[key][:_MAX_EVIDENCE_KEYS],
        }
        if resolver is not None:
            # The internal namespace tag stays internal: the example's key is the
            # compared value, and ``mapping`` says whether it is a canonical ID.
            label = key_label(key, resolver)
            example["key"] = label["key"]
            if "mapping" in label:
                example["mapping"] = label["mapping"]
        examples.append(example)
    return examples


def _key_sort_key(key: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(f"{type(value).__name__}:{value!s}" for value in key)
