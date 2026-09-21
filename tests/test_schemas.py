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


def test_every_fair_rule_validates_against_the_rule_schema():
    yaml = pytest.importorskip("yaml", reason="profiles need the 'fair' extra")
    schema = _schema("fair-rule.schema.json")
    rules_dir = SCHEMA_DIR.parent / "src" / "data2agent" / "profiles" / "fair" / "rules"
    paths = sorted(rules_dir.glob("*.yaml"))
    assert paths, "the FAIR profile must ship rules"
    for path in paths:
        jsonschema.validate(yaml.safe_load(path.read_text(encoding="utf-8")), schema)


def test_the_rule_schema_rejects_a_rule_that_cannot_say_unknown():
    schema = _schema("fair-rule.schema.json")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "id": "X1-RULE",
                "principle": "F1",
                "question": "Does the thing hold?",
                "check": {"type": "metadata_presence"},
                "allowed_results": ["pass", "fail"],
            },
            schema,
        )


def test_a_real_assessment_validates_against_the_assessment_schema(ingested):
    pytest.importorskip("yaml", reason="profiles need the 'fair' extra")
    from data2agent.mcp import DatasetService

    service = DatasetService(ingested.output_dir, mode="fair-deterministic")
    jsonschema.validate(service.run_fair_check(), _schema("assessment.schema.json"))


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
