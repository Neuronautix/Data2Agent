"""The published schemas are the contract; the pipeline must satisfy them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema", reason="schema validation needs the 'dev' extra")

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def test_manifest_matches_its_schema(ingested):
    jsonschema.validate(ingested.manifest, _schema("dataset-manifest.schema.json"))


def test_evidence_matches_its_schema(ingested):
    jsonschema.validate(ingested.evidence.as_dict(), _schema("evidence.schema.json"))


def test_schemas_are_themselves_valid():
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)


def test_assessment_schema_rejects_an_unsourced_result():
    """The v0.2 contract must not allow a FAIR verdict with no evidence."""
    schema = _schema("assessment.schema.json")
    invalid = {
        "assessment_version": "0.1.0",
        "dataset_id": "sha256:" + "0" * 64,
        "profile": {"id": "fair", "version": "0.1.0"},
        "generator": {"mode": "structured"},
        "results": [
            {"rule_id": "F1-PID-METADATA", "principle": "F1", "result": "pass", "evidence": []}
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)


def test_assessment_schema_accepts_unknown_as_a_first_class_result():
    schema = _schema("assessment.schema.json")
    valid = {
        "assessment_version": "0.1.0",
        "dataset_id": "sha256:" + "0" * 64,
        "profile": {"id": "fair", "version": "0.1.0"},
        "generator": {"mode": "structured", "host": "claude-code", "model": "example"},
        "results": [
            {
                "rule_id": "F1-PID-METADATA",
                "principle": "F1",
                "result": "unknown",
                "evidence": ["clm_0123456789abcdef"],
                "rationale": "no persistent identifier was found in any recognised metadata file",
            }
        ],
        "unsupported_claims": [],
    }
    jsonschema.validate(valid, schema)
