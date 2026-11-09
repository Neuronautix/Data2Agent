"""The runtime ontology-constraint engine: scope, canonical IRIs, abstention, pinning."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from data2agent.semantic.constraints import (
    FORMAL_ONLY_NOTE,
    MappingPolicy,
    OntologyRuntime,
    OntologySnapshot,
    SemanticError,
    VocabularyUnavailable,
    sha256_of,
    unavailable_outcome,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "semantic"
SNAPSHOT = EXAMPLE / "snapshot.json"
POLICY = EXAMPLE / "policy.json"
MUS = "http://purl.obolibrary.org/obo/NCBITaxon_10090"
RAT = "http://purl.obolibrary.org/obo/NCBITaxon_10116"
HUMAN = "http://purl.obolibrary.org/obo/NCBITaxon_9606"
GENUS_MUS = "http://purl.obolibrary.org/obo/NCBITaxon_10088"
MALE = "http://purl.obolibrary.org/obo/PATO_0000384"


@pytest.fixture
def runtime() -> OntologyRuntime:
    return OntologyRuntime.load(SNAPSHOT, POLICY)


def _snapshot_with(extra_terms: list[dict]) -> dict:
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    data["terms"] = data["terms"] + extra_terms
    return data


def test_branch_restriction_admits_rodent_species_only(runtime):
    admissible = set(runtime.admissible_terms("species"))
    assert {MUS, RAT} <= admissible
    assert HUMAN not in admissible  # outside the Rodentia branch
    assert GENUS_MUS not in admissible  # inside the branch but explicitly excluded


def test_explicit_term_set_restriction(runtime):
    assert set(runtime.admissible_terms("sex")) == {
        MALE,
        "http://purl.obolibrary.org/obo/PATO_0000383",
    }


def test_whole_ontology_restriction():
    data = json.loads(POLICY.read_text(encoding="utf-8"))
    data["fields"]["any_taxon"] = {"datatype": "iri", "allowed": {"ontologies": ["ncbitaxon"]}}
    snapshot = OntologySnapshot.load(SNAPSHOT)
    runtime = OntologyRuntime(snapshot, MappingPolicy(data, snapshot))
    assert HUMAN in runtime.admissible_terms("any_taxon")
    assert MALE not in runtime.admissible_terms("any_taxon")


def test_search_never_returns_out_of_scope_terms(runtime):
    result = runtime.search_ontology_terms("human", "species", top_k=5)
    assert result["candidates"] == []
    assert result["out_of_scope_matches"] == 1  # Homo sapiens matched, withheld
    mouse = runtime.search_ontology_terms("mouse", "species", top_k=3)
    assert mouse["candidates"][0]["iri"] == MUS
    assert mouse["candidates"][0]["match"] == "exact_synonym"
    assert mouse["candidates"][0]["version"] == "extract-2026-11-08"


def test_search_is_deterministic_and_bounded(runtime):
    first = runtime.search_ontology_terms("Rattus norvegicus", "species", top_k=2)
    second = runtime.search_ontology_terms("Rattus norvegicus", "species", top_k=2)
    assert first == second and len(first["candidates"]) <= 2
    with pytest.raises(SemanticError):
        runtime.search_ontology_terms("x", "species", top_k=0)


def test_canonical_iri_preserved_and_curie_flagged(runtime):
    result = runtime.validate_semantic_mapping(
        {"field": "species", "decision": "map", "value": "NCBITaxon:10090"}
    )["results"][0]
    assert result["status"] == "admissible"
    assert result["canonical_values"] == [MUS]
    assert result["warnings"][0]["code"] == "non_canonical_form"


def test_label_is_not_an_identifier(runtime):
    result = runtime.validate_semantic_mapping(
        {"field": "species", "decision": "map", "value": "Mus musculus"}
    )
    assert result["formally_valid"] is False
    assert result["results"][0]["errors"][0]["code"] == "label_not_iri"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (HUMAN, "outside_scope"),
        (GENUS_MUS, "excluded"),
        ("http://purl.obolibrary.org/obo/NCBITaxon_99999999", "unknown_iri"),
        (MALE, "outside_scope"),
    ],
)
def test_invalid_terms_rejected(runtime, value, code):
    result = runtime.validate_semantic_mapping(
        {"field": "species", "decision": "map", "value": value}
    )
    assert result["results"][0]["status"] == "invalid"
    assert result["results"][0]["errors"][0]["code"] == code
    assert result["note"] == FORMAL_ONLY_NOTE


def test_cardinality_and_literal_value_sets(runtime):
    two = runtime.validate_semantic_mapping(
        {"field": "species", "decision": "map", "value": [MUS, RAT]}
    )
    assert two["results"][0]["errors"][0]["code"] == "cardinality_violation"
    bad = runtime.validate_semantic_mapping({"field": "license", "decision": "map", "value": "MIT"})
    assert bad["results"][0]["errors"][0]["code"] == "literal_not_allowed"
    good = runtime.validate_semantic_mapping(
        {"field": "license", "decision": "map", "value": "CC0-1.0"}
    )
    assert good["formally_valid"] is True


def test_explicit_abstention_is_valid_only_with_a_reason(runtime):
    ok = runtime.validate_semantic_mapping(
        {"field": "sex", "decision": "abstain", "value": None, "reason": "codes 1/2 have no legend"}
    )
    assert ok["formally_valid"] is True and ok["counts"]["unresolved"] == 1
    silent = runtime.validate_semantic_mapping({"field": "sex", "decision": "needs_review"})
    assert silent["results"][0]["errors"][0]["code"] == "missing_reason"
    mixed = runtime.validate_semantic_mapping(
        {"field": "sex", "decision": "abstain", "value": MALE, "reason": "x"}
    )
    assert mixed["results"][0]["errors"][0]["code"] == "value_with_abstention"


def test_deprecated_terms_are_refused_with_their_replacement():
    old = "http://purl.obolibrary.org/obo/NCBITaxon_0000001"
    data = _snapshot_with(
        [
            {
                "iri": old,
                "ontology": "ncbitaxon",
                "kind": "class",
                "label": "obsolete mouse",
                "parents": ["http://purl.obolibrary.org/obo/NCBITaxon_9989"],
                "deprecated": True,
                "replaced_by": MUS,
            }
        ]
    )
    snapshot = OntologySnapshot(data)
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["snapshot"]["sha256"] = snapshot.sha256
    runtime = OntologyRuntime(snapshot, MappingPolicy(policy, snapshot))
    result = runtime.validate_semantic_mapping(
        {"field": "species", "decision": "map", "value": old}
    )
    error = result["results"][0]["errors"][0]
    assert error["code"] == "deprecated_term" and error["replaced_by"] == MUS


def test_term_context(runtime):
    context = runtime.get_term_context("NCBITaxon:10090")
    assert context["found"] and context["canonical_iri"] == MUS and context["non_canonical_input"]
    assert "http://purl.obolibrary.org/obo/NCBITaxon_9989" in context["ancestors"]
    assert context["admissible_for_fields"] == ["species"]
    assert runtime.get_term_context("http://example.org/nothing") == {
        "iri": "http://example.org/nothing",
        "found": False,
    }


def test_constraints_profile_lists_scope(runtime):
    constraints = runtime.get_mapping_constraints({"fields": ["species"]})
    field = constraints["fields"]["species"]
    assert list(constraints["fields"]) == ["species"]
    assert field["cardinality"] == {"min": 1, "max": 1}
    assert {t["iri"] for t in field["admissible_terms"]} == {MUS, RAT}
    assert constraints["snapshot"]["sha256"] == OntologySnapshot.load(SNAPSHOT).sha256
    with pytest.raises(SemanticError):
        runtime.get_mapping_constraints({"fields": ["no_such_field"]})


def test_missing_or_tampered_vocabulary_is_unavailable(tmp_path):
    with pytest.raises(VocabularyUnavailable):
        OntologyRuntime.load(tmp_path / "absent.json", POLICY)
    with pytest.raises(VocabularyUnavailable):
        OntologySnapshot.load(SNAPSHOT, expected_sha256="0" * 64)
    tampered = _snapshot_with([])
    tampered["terms"][0]["label"] = "edited"
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(VocabularyUnavailable, match="bound to snapshot"):
        OntologyRuntime.load(path, POLICY)
    outcome = unavailable_outcome(["species"], VocabularyUnavailable("offline"))
    assert outcome[0]["decision"] == "needs_review" and outcome[0]["value"] is None


def test_policy_must_be_closed_world_and_reference_only_snapshot_terms():
    snapshot = OntologySnapshot.load(SNAPSHOT)
    data = json.loads(POLICY.read_text(encoding="utf-8"))
    with pytest.raises(SemanticError, match="closed_world"):
        MappingPolicy({**data, "closed_world": False}, snapshot)
    data["fields"]["species"]["allowed"]["terms"] = ["NCBITaxon:123456789"]
    with pytest.raises(SemanticError, match="not in the snapshot"):
        MappingPolicy(data, snapshot)


def test_shacl_hook_is_reported_not_assumed(runtime):
    report = runtime.validate_semantic_mapping(
        {"field": "species", "decision": "map", "value": MUS}
    )
    assert report["shacl"] == {"status": "not_run"}
    failing = runtime.validate_semantic_mapping(
        {"field": "species", "decision": "map", "value": MUS},
        shacl=lambda items: {"conforms": False},
    )
    assert failing["shacl"]["conforms"] is False and failing["formally_valid"] is False


def test_snapshot_digest_is_canonical_json():
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert OntologySnapshot(data).sha256 == sha256_of(data)


def test_engine_module_is_stdlib_only_and_vendorable():
    """constraints.py is copied byte-identically into other repositories: no package imports."""
    path = (
        Path(__file__).resolve().parents[2] / "src" / "data2agent" / "semantic" / "constraints.py"
    )
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and (node.module or "").split(".")[0] in stdlib
        elif isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in stdlib for alias in node.names)
