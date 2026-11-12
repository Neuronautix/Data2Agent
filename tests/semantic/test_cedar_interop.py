"""Adversarial offline coverage for scheme membership and fail-closed CEDAR reports."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from data2agent.semantic.cedar import compile_profile, export_class_policy, import_template
from data2agent.semantic.constraints import (
    MappingPolicy,
    OntologyRuntime,
    OntologySnapshot,
    SemanticError,
)

CEDAR = Path(__file__).parent / "fixtures" / "cedar" / "SimpleTemplate.json"


@pytest.fixture
def profile(tmp_path):
    rdf = """@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
      @prefix ex: <https://example.org/> .
      ex:scheme a skos:ConceptScheme .
      ex:a a skos:Concept ; skos:inScheme ex:scheme ; skos:prefLabel "A" .
      ex:b a skos:Concept ; skos:inScheme ex:scheme ; skos:broader ex:a .
    """
    other = """@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
      @prefix ex: <https://example.org/> .
      ex:other a skos:ConceptScheme .
      ex:c a skos:Concept ; skos:inScheme ex:other .
    """
    owl = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
      <https://example.org/property> a owl:DatatypeProperty ."""
    sources = {}
    for name, text in (("ontology", owl), ("modality", rdf), ("technology", other)):
        p = tmp_path / f"{name}.ttl"
        p.write_text(text, encoding="utf-8")
        sources[name] = {
            "path": p.name,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "version": "v1",
        }
    document = {
        "profile_id": "hcm-recording-minimal",
        "version": "1",
        "sources": sources,
        "fields": {
            "modality": {
                "entity": "profile annotation",
                "property": None,
                "value_type": "mapping",
                "min_count": 0,
                "max_count": 1,
                "policy": {
                    "scheme": "https://example.org/scheme",
                    "source": "modality",
                    "kind": "skos:Concept",
                    "hierarchy": "skos:broader",
                    "allowed": ["https://example.org/a"],
                },
            },
            "sampling_rate": {
                "value_type": "quantity",
                "min_count": 0,
                "max_count": 1,
                "policy": {},
            },
        },
    }
    return tmp_path, document


def _compile(profile):
    root, doc = profile
    return compile_profile(yaml.safe_dump(doc).encode(), root)


def test_scheme_intersects_explicit_set_and_blocks_quantity(profile):
    result = _compile(profile)
    snapshot = OntologySnapshot(result["snapshot"])
    runtime = OntologyRuntime(snapshot, MappingPolicy(result["policy"], snapshot))
    assert runtime.admissible_terms("modality") == ("https://example.org/a",)
    for value in ("https://example.org/b", "https://example.org/c", "https://example.org/missing"):
        verdict = runtime.validate_semantic_mapping(
            [{"field": "modality", "decision": "map", "value": value}]
        )
        assert verdict["results"][0]["status"] == "invalid"
    assert result["fields"]["sampling_rate"]["codes"] == ["structured_quantity_unsupported"]
    assert result["status"] == "blocked" and result["artifact"] is None
    assert _compile(profile) == result
    invalid_policy = copy.deepcopy(result["policy"])
    invalid_policy["fields"]["modality"]["allow_deprecated"] = True
    with pytest.raises(SemanticError, match="deprecated"):
        MappingPolicy(invalid_policy, snapshot)


def test_source_drift_and_wrong_scheme_fail_closed(profile):
    root, document = profile
    document["fields"]["modality"]["policy"]["allowed"] = ["https://example.org/c"]
    with pytest.raises(SemanticError, match="invalid scheme member"):
        _compile(profile)
    document["fields"]["modality"]["policy"]["allowed"] = ["https://example.org/a"]
    (root / "modality.ttl").write_text("modified", encoding="utf-8")
    with pytest.raises(SemanticError, match="digest"):
        _compile(profile)


def test_unknown_profile_constraint_is_blocked(profile):
    profile[1]["fields"]["modality"]["policy"]["newConstraint"] = True
    # A report never emits a policy that could discard an unknown constraint.
    with pytest.raises(SemanticError, match="at least one field"):
        _compile(profile)


def test_official_cedar_fixture_is_pinned_and_blocked(profile):
    raw = CEDAR.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == "22a00307d2ed8b8116b709e34b3e7dcaa46d4df8a4f0eb295025fefcbad71798"
    artifact = json.loads(raw)
    source = {"id": artifact["@id"], "version": artifact["pav:version"], "raw_sha256": digest}
    snapshot = OntologySnapshot(_compile(profile)["snapshot"])
    report = import_template(raw, snapshot, {}, expected_source=source)
    assert report["status"] == "blocked" and report["policy"] is None
    assert (
        "unsupported_constraints"
        in report["fields"]["Text Field - Min/Max String Lengths"]["codes"]
    )
    with pytest.raises(SemanticError, match="identity"):
        import_template(raw, snapshot, {}, expected_source={**source, "version": "changed"})


def test_explicit_classes_need_bound_approval_and_refuse_drift(profile):
    snapshot_data = _compile(profile)["snapshot"]
    snapshot_data["ontologies"]["DOID"] = {"version": "release-1"}
    snapshot_data["sources"].append(
        {"ontology": "DOID", "version": "release-1", "sha256": "f" * 64}
    )
    for iri in (
        "http://purl.obolibrary.org/obo/DOID_530",
        "http://purl.obolibrary.org/obo/DOID_2825",
        "http://purl.obolibrary.org/obo/DOID_850",
    ):
        snapshot_data["terms"].append(
            {"iri": iri, "ontology": "DOID", "kind": "class", "parents": []}
        )
    snapshot_data["terms"].append(
        {
            "iri": "https://example.org/Entity",
            "ontology": "ontology",
            "kind": "class",
            "parents": [],
        }
    )
    snapshot_data["terms"].append(
        {
            "iri": "https://example.org/objectProperty",
            "ontology": "ontology",
            "kind": "property",
            "property_type": "object",
            "parents": [],
        }
    )
    snapshot = OntologySnapshot(snapshot_data)
    reference = json.loads(CEDAR.read_bytes())
    key = "Classes Value Field"
    # Preserve upstream field shape while making a one-field synthetic test template.
    original_order = set(reference["_ui"]["order"])
    reference["_ui"] = {"order": [key]}
    original_properties = reference["properties"]
    context_schema = original_properties["@context"]
    context_schema["properties"] = {
        k: v for k, v in context_schema["properties"].items() if k not in original_order or k == key
    }
    context_schema["properties"][key] = {"enum": ["https://example.org/objectProperty"]}
    reference["properties"] = {
        k: v for k, v in original_properties.items() if k not in original_order or k == key
    }
    reference["required"] = [
        k for k in reference["required"] if k not in original_order or k == key
    ]
    reference["additionalProperties"] = False
    raw = json.dumps(reference, sort_keys=True).encode()
    source = {
        "id": reference["@id"],
        "version": reference["pav:version"],
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "ontologies": {"DOID": {"version": "release-1", "sha256": "f" * 64}},
    }
    targets = {
        key: {
            "property": "https://example.org/objectProperty",
            "entity": "https://example.org/Entity",
            "datatype": "iri",
        }
    }
    first = import_template(raw, snapshot, targets, expected_source=source)
    assert first["status"] == "exact" and not first["approved"] and first["policy"] is None, (
        first["codes"],
        first["fields"][key]["codes"],
    )
    approved = import_template(
        raw,
        snapshot,
        targets,
        expected_source=source,
        approval={"binding_sha256": first["binding_sha256"], "approved_by": "reviewer"},
    )
    assert approved["approved"] and approved["policy"] == first["candidate_policy"]
    approval = {"binding_sha256": first["binding_sha256"], "approved_by": "reviewer"}
    exported = export_class_policy(
        approved["policy"], raw, snapshot, targets, expected_source=source, approval=approval
    )
    assert (
        exported["artifact"] == reference
        and exported["artifact_raw_sha256"] == source["raw_sha256"]
    )
    for mutation in (
        "wrong_type",
        "wrong_artifact_type",
        "wrong_required",
        "context_constraint",
        "unknown_constraint",
        "required_value",
    ):
        bad = copy.deepcopy(reference)
        if mutation == "wrong_type":
            bad["type"] = "array"
        elif mutation == "wrong_artifact_type":
            bad["@type"] = "https://schema.metadatacenter.org/core/TemplateElement"
        elif mutation == "wrong_required":
            bad["required"] = ["nonexistent"]
        elif mutation == "context_constraint":
            bad["properties"]["@context"]["minProperties"] = 999
        elif mutation == "unknown_constraint":
            bad["properties"][key]["_valueConstraints"]["newRestriction"] = True
        else:
            bad["properties"][key]["_valueConstraints"]["requiredValue"] = True
        bad_raw = json.dumps(bad, sort_keys=True).encode()
        bad_source = {**source, "raw_sha256": hashlib.sha256(bad_raw).hexdigest()}
        assert (
            import_template(bad_raw, snapshot, targets, expected_source=bad_source)["status"]
            == "blocked"
        )
    bad = copy.deepcopy(reference)
    bad["_ui"]["order"] = [[]]
    bad_raw = json.dumps(bad, sort_keys=True).encode()
    with pytest.raises(SemanticError):
        import_template(
            bad_raw,
            snapshot,
            targets,
            expected_source={**source, "raw_sha256": hashlib.sha256(bad_raw).hexdigest()},
        )
    changed_policy = {**approved["policy"], "version": "changed"}
    assert (
        export_class_policy(
            changed_policy, raw, snapshot, targets, expected_source=source, approval=approval
        )["status"]
        == "blocked"
    )
    assert not import_template(
        raw,
        snapshot,
        targets,
        expected_source={
            **source,
            "ontologies": {"DOID": {"version": "changed", "sha256": "f" * 64}},
        },
        approval={"binding_sha256": first["binding_sha256"], "approved_by": "reviewer"},
    )["approved"]
    wrong_target = {
        key: {
            "property": "https://example.org/property",
            "entity": "https://example.org/Entity",
            "datatype": "iri",
        }
    }
    assert (
        import_template(raw, snapshot, wrong_target, expected_source=source)["status"] == "blocked"
    )
