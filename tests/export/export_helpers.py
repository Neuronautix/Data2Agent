"""Constants and helpers shared by the annotation-exporter tests (synthetic data only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FAMILY = "synthfam"
DATASET_PATH = f"data/original/{FAMILY}"
AGENT_MODEL = "test-model-1"
CREATED_AT = "2026-01-02T03:04:05Z"

LOCATOR = r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/\-#\[\]=]+$"

STANDIN_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "annotation_id",
        "dataset",
        "annotation_type",
        "target",
        "criterion",
        "judgement",
        "evidence",
        "provenance",
        "review",
    ],
    "properties": {
        "schema_version": {"const": "0.1.0"},
        "annotation_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"},
        "dataset": {
            "type": "object",
            "required": ["dataset_family_id", "variant"],
            "properties": {"variant": {"enum": ["original", "curated"]}},
        },
        "annotation_type": {
            "enum": [
                "fair_assessment",
                "quality_issue",
                "expected_improvement",
                "curation_validation",
            ]
        },
        "target": {
            "type": "object",
            "required": ["kind", "path"],
            "properties": {
                "kind": {"enum": ["dataset", "file", "record", "field", "metadata_element"]},
                "path": {"type": "string", "pattern": LOCATOR},
            },
        },
        "fair_dimension": {"enum": ["findable", "accessible", "interoperable", "reusable"]},
        "criterion": {"type": "object", "required": ["id", "description"]},
        "judgement": {
            "type": "object",
            "required": ["label"],
            "properties": {
                "label": {
                    "enum": [
                        "conformant",
                        "non_conformant",
                        "partial",
                        "not_applicable",
                        "insufficient_evidence",
                    ]
                },
                "rationale": {"type": "string", "minLength": 1},
            },
        },
        "evidence": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["locator", "description"],
                "properties": {
                    "locator": {"type": "string", "pattern": LOCATOR},
                    "description": {"type": "string", "minLength": 1},
                    "checksum": {
                        "type": "object",
                        "required": ["algorithm", "value"],
                        "properties": {
                            "algorithm": {"const": "sha256"},
                            "value": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                        },
                    },
                },
            },
        },
        "provenance": {
            "type": "object",
            "required": ["method", "created_by", "created_at"],
            "allOf": [
                {
                    "if": {"properties": {"method": {"const": "agent_proposal"}}},
                    "then": {"required": ["agent_model"]},
                }
            ],
        },
        "review": {"type": "object", "required": ["status"]},
    },
    "allOf": [
        {
            "if": {
                "properties": {"annotation_type": {"const": "fair_assessment"}},
                "required": ["annotation_type"],
            },
            "then": {"required": ["fair_dimension"]},
        },
        {
            "if": {
                "properties": {
                    "provenance": {"properties": {"method": {"const": "agent_proposal"}}}
                },
                "required": ["provenance"],
            },
            "then": {
                "properties": {
                    "review": {
                        "properties": {"status": {"enum": ["draft", "in_review", "rejected"]}}
                    }
                }
            },
        },
    ],
}


def result(rule_id: str, principle: str, outcome: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "rule_id": rule_id,
        "principle": principle,
        "result": outcome,
        "evidence": extra.pop("evidence", [{"check": "file.checksum", "result": "x"}]),
        "inferred": False,
    }
    payload.update(extra)
    return payload


def write_profile(root: Path, profile_id: str, rules: dict[str, tuple[str, str]]) -> Path:
    """A synthetic profile directory: ``rules`` maps rule id -> (principle, question)."""
    base = root / profile_id
    (base / "rules").mkdir(parents=True)
    (base / "profile.yaml").write_text(
        f"id: {profile_id}\nversion: 0.1.0\ntitle: {profile_id}\ndescription: synthetic\n",
        encoding="utf-8",
    )
    for rule_id, (principle, question) in rules.items():
        (base / "rules" / f"{rule_id}.yaml").write_text(
            f"id: {rule_id}\nprinciple: '{principle}'\nquestion: >\n  {question}\n"
            "check:\n  type: whatever\nimplemented: false\nnot_implemented_reason: synthetic\n",
            encoding="utf-8",
        )
    return root


def read_proposed(dataspace: Path) -> dict[str, dict[str, Any]]:
    folder = dataspace / "annotations" / "proposed"
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(folder.glob("*.json"))
    }
