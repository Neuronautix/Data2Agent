"""Captured RDF is validated offline, from frozen bodies, and served as one more record."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("rdflib")

from data2agent import rdf_validation  # noqa: E402
from data2agent.cli import main  # noqa: E402
from data2agent.mcp import DatasetService  # noqa: E402
from data2agent.snapshot import Snapshot, SnapshotError, collect, validate_spec  # noqa: E402

DATA = "https://repository.example/studies/1.rdf"
VOCAB = "https://vocabulary.example/ontology.owl"
NS = "https://vocabulary.example/ontology#"

VOCAB_TTL = f"""
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix ex: <{NS}> .
ex:Study a owl:Class .
ex:Assay a owl:Class .
ex:hasPart a owl:ObjectProperty .
ex:title a owl:DatatypeProperty .
""".encode()

GOOD_TTL = f"""
@prefix ex: <{NS}> .
@prefix dc: <http://purl.org/dc/terms/> .
<https://repository.example/studies/1> a ex:Study ;
    ex:hasPart <https://repository.example/assays/7> ;
    ex:title "Example study" ;
    dc:creator "Someone" .
<https://repository.example/assays/7> a ex:Assay .
""".encode()

UNDEFINED_TTL = f"""
@prefix ex: <{NS}> .
<https://repository.example/studies/1> a ex:Study ;
    ex:hasWidget <https://repository.example/assays/7> .
<https://repository.example/assays/7> a ex:Gadget .
""".encode()

GOOD_RDFXML = f"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:ex="{NS}">
  <ex:Study rdf:about="https://repository.example/studies/1">
    <ex:title>Example study</ex:title>
  </ex:Study>
</rdf:RDF>
""".encode()


def _observed(url, body, *, truncated=False, content_type="text/turtle"):
    return {
        "source": url,
        "observed_at": "2026-11-13T10:00:00Z",
        "result": "reached",
        "status_code": 200,
        "final_url": url,
        "content_type": content_type,
        "chain": [],
        "body": body,
        "truncated": truncated,
        "truncation_reason": "body_limit" if truncated else None,
    }


FAILED = {"observed_at": "2026-11-13T10:00:01Z", "result": "network_error", "chain": []}


def _spec(data_format="turtle", vocab_format="turtle", **overrides):
    spec = {
        "snapshot_spec_version": "1.1",
        "dataset_label": "example-published",
        "requests": [
            {"id": "study-rdf", "url": DATA, "accept": "text/turtle", "purpose": "I1/1"},
            {"id": "vocab", "url": VOCAB, "accept": "text/turtle", "purpose": "I1/3 vocabulary"},
        ],
        "validations": [
            {
                "id": "study-rdf-validation",
                "record": "study-rdf",
                "format": data_format,
                "purpose": "I1/3 validation of the representation",
                "vocabularies": [{"record": "vocab", "format": vocab_format, "namespace": NS}],
            }
        ],
    }
    spec.update(overrides)
    return spec


def _fetch(responses):
    calls = []

    def fetch(url, *, accept, max_body):
        calls.append(url)
        return dict(responses[url])

    fetch.calls = calls
    return fetch


def _collect(tmp_path, data, vocab=None, spec=None):
    vocab = vocab if vocab is not None else _observed(VOCAB, VOCAB_TTL)
    out = tmp_path / "snap"
    collect(spec or _spec(), out, fetch=_fetch({DATA: data, VOCAB: vocab}))
    snapshot = Snapshot(out)
    report = json.loads(snapshot.read("study-rdf-validation", limit=32_768)["text"])
    return out, snapshot, report


def test_conforming_rdf_is_validated_offline_and_served_as_a_record(tmp_path):
    out, snapshot, report = _collect(tmp_path, _observed(DATA, GOOD_TTL))
    data = json.loads((out / "snapshot.json").read_text(encoding="utf-8"))
    assert data["snapshot_format_version"] == "1.2"
    assert data["collector"]["validation"]["method"] == "d2a-rdf-validation-1"
    record = data["records"][-1]
    assert record["kind"] == "rdf_validation" and record["validates"] == "study-rdf"
    assert record["result"] == "conforms" and "url" not in record
    assert report["result"] == "conforms"
    assert report["syntax"] == {"parsed": True, "error": None, "triples": 5}
    vocab = report["vocabularies"][0]
    assert vocab["terms_defined"] == 4 and vocab["terms_used"] == 4
    assert vocab["undefined_terms"] == []
    # Other namespaces (here Dublin Core) are not checked against this vocabulary.
    assert report["validated_body_sha256"] == data["records"][0]["body_sha256"]
    listed = snapshot.describe()["records"]
    assert [r["id"] for r in listed] == ["study-rdf", "vocab", "study-rdf-validation"]


def test_undefined_terms_are_listed_and_do_not_conform(tmp_path):
    _, _, report = _collect(tmp_path, _observed(DATA, UNDEFINED_TTL))
    assert report["result"] == "does_not_conform"
    assert report["vocabularies"][0]["undefined_terms"] == [NS + "Gadget", NS + "hasWidget"]


def test_a_parse_error_does_not_conform(tmp_path):
    _, _, report = _collect(tmp_path, _observed(DATA, b"@prefix ex: <broken . ex:a ex:b"))
    assert report["result"] == "does_not_conform"
    assert report["syntax"]["parsed"] is False and report["syntax"]["error"]


def test_rdfxml_data_against_a_turtle_vocabulary(tmp_path):
    spec = _spec(data_format="rdfxml")
    _, _, report = _collect(tmp_path, _observed(DATA, GOOD_RDFXML), spec=spec)
    assert report["result"] == "conforms" and report["syntax"]["triples"] == 2


OWLXML_BASE = NS.rstrip("#")
VOCAB_OWLXML = f"""<?xml version="1.0"?>
<Ontology xmlns="http://www.w3.org/2002/07/owl#" xml:base="{OWLXML_BASE}"
          ontologyIRI="{OWLXML_BASE}">
  <Prefix name="ex" IRI="{NS}"/>
  <Declaration><Class IRI="#Study"/></Declaration>
  <Declaration><Class abbreviatedIRI="ex:Assay"/></Declaration>
  <Declaration><ObjectProperty IRI="{NS}hasPart"/></Declaration>
  <Declaration><DataProperty IRI="#title"/></Declaration>
  <SubClassOf><Class IRI="#Assay"/><Class IRI="#Thing"/></SubClassOf>
</Ontology>
""".encode()


def test_an_owlxml_vocabulary_is_read_from_its_declarations(tmp_path):
    spec = _spec(vocab_format="owlxml")
    vocab = _observed(VOCAB, VOCAB_OWLXML, content_type="application/xml")
    _, _, report = _collect(tmp_path, _observed(DATA, GOOD_TTL), vocab, spec=spec)
    assert report["result"] == "conforms"
    assert report["vocabularies"][0]["terms_defined"] == 4


def test_an_owlxml_vocabulary_lists_what_it_does_not_declare(tmp_path):
    spec = _spec(vocab_format="owlxml")
    vocab = _observed(VOCAB, VOCAB_OWLXML, content_type="application/xml")
    _, _, report = _collect(tmp_path, _observed(DATA, UNDEFINED_TTL), vocab, spec=spec)
    assert report["result"] == "does_not_conform"
    assert report["vocabularies"][0]["undefined_terms"] == [NS + "Gadget", NS + "hasWidget"]


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0"?><!DOCTYPE o [<!ENTITY a "a">]><Ontology/>',
        b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>",  # not OWL/XML
        b"<Ontology",  # broken XML
    ],
)
def test_an_unusable_owlxml_vocabulary_is_not_validated(tmp_path, body):
    spec = _spec(vocab_format="owlxml")
    vocab = _observed(VOCAB, body, content_type="application/xml")
    _, _, report = _collect(tmp_path, _observed(DATA, GOOD_TTL), vocab, spec=spec)
    assert report["result"] == "not_validated"


RELATIVE_VOCAB_RDFXML = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="#Study"/>
  <owl:Class rdf:about="#Assay"/>
  <owl:ObjectProperty rdf:about="#hasPart"/>
  <owl:DatatypeProperty rdf:about="#title"/>
</rdf:RDF>
"""


def test_relative_iris_resolve_against_the_url_the_body_was_served_from(tmp_path):
    vocab = {
        **_observed(VOCAB, RELATIVE_VOCAB_RDFXML, content_type="application/rdf+xml"),
        "final_url": NS.rstrip("#"),  # e.g. after a redirect to the ontology IRI
    }
    spec = _spec(vocab_format="rdfxml")
    _, _, report = _collect(tmp_path, _observed(DATA, GOOD_TTL), vocab, spec=spec)
    assert report["result"] == "conforms"
    assert report["vocabularies"][0]["terms_defined"] == 4
    assert report["base_iri"] == DATA


@pytest.mark.parametrize(
    "role, status, body",
    [
        ("data", 404, b"<html>Not found</html>"),
        ("data", 503, b""),  # an empty error body would parse as empty Turtle
        ("vocab", 404, b"<html>Not found</html>"),
    ],
)
def test_an_http_error_response_is_never_validated_as_the_document(tmp_path, role, status, body):
    url = DATA if role == "data" else VOCAB
    error_page = {**_observed(url, body, content_type="text/html"), "result": "not_reached"}
    error_page["status_code"] = status
    data = error_page if role == "data" else _observed(DATA, GOOD_TTL)
    vocab = error_page if role == "vocab" else None
    _, _, report = _collect(tmp_path, data, vocab)
    assert report["result"] == "not_validated"
    assert any("not reached" in reason for reason in report["reasons"])


def test_owlxml_is_accepted_for_vocabularies_only():
    spec = _spec(data_format="owlxml")
    with pytest.raises(SnapshotError, match="format must be one of"):
        validate_spec(spec)


@pytest.mark.parametrize(
    "data, vocab",
    [
        (_observed(DATA, GOOD_TTL, truncated=True), None),  # a cut document
        (FAILED, None),  # no document
        (None, FAILED),  # no vocabulary
        (None, _observed(VOCAB, b"not rdf at all {")),  # an unusable vocabulary
    ],
)
def test_missing_or_partial_input_is_not_validated_never_a_finding(tmp_path, data, vocab):
    _, _, report = _collect(tmp_path, data or _observed(DATA, GOOD_TTL), vocab)
    assert report["result"] == "not_validated" and report["reasons"]


def test_rdfxml_declaring_entities_is_refused_and_not_validated(tmp_path):
    body = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]>'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'
    )
    _, _, report = _collect(tmp_path, _observed(DATA, body), spec=_spec(data_format="rdfxml"))
    assert report["result"] == "not_validated"
    assert report["syntax"] is None and "entities" in report["reasons"][0]


@pytest.mark.parametrize(
    "change",
    [
        {"snapshot_spec_version": "1.0"},  # validations need spec 1.1
        {"validations": []},
        {"validations": [{"id": "x"}]},
    ],
)
def test_invalid_validation_specs_are_refused_before_any_fetch(tmp_path, change):
    fetch = _fetch({})
    with pytest.raises(SnapshotError):
        collect(_spec(**change), tmp_path / "snap", fetch=fetch)
    assert fetch.calls == [] and not (tmp_path / "snap").exists()


@pytest.mark.parametrize(
    "field, value",
    [
        ("id", "study-rdf"),  # collides with a request id
        ("id", "../escape"),
        ("record", "unknown"),
        ("format", "json-ld"),  # could fetch remote contexts
        ("vocabularies", [{"record": "vocab", "format": "turtle", "namespace": "urn:x"}]),
        ("vocabularies", [{"record": "nope", "format": "turtle", "namespace": NS}]),
        ("vocabularies", [{"record": "vocab", "format": "turtle"}]),
    ],
)
def test_each_validation_field_is_checked(field, value):
    spec = _spec()
    spec["validations"][0][field] = value
    with pytest.raises(SnapshotError):
        validate_spec(spec)


def test_without_rdflib_the_spec_is_refused_before_any_fetch(monkeypatch, tmp_path):
    def missing():
        raise rdf_validation.RDFValidationUnavailable("RDF validation needs rdflib")

    monkeypatch.setattr(rdf_validation, "require_rdflib", missing)
    fetch = _fetch({})
    with pytest.raises(SnapshotError, match="needs rdflib"):
        collect(_spec(), tmp_path / "snap", fetch=fetch)
    assert fetch.calls == []


def test_spec_1_0_without_validations_is_unchanged(tmp_path):
    spec = _spec(snapshot_spec_version="1.0")
    del spec["validations"]
    out = tmp_path / "snap"
    collect(spec, out, fetch=_fetch({DATA: _observed(DATA, GOOD_TTL), VOCAB: FAILED}))
    data = json.loads((out / "snapshot.json").read_text(encoding="utf-8"))
    assert "validation" not in data["collector"]
    assert [r["id"] for r in data["records"]] == ["study-rdf", "vocab"]


def _rewrite(out, edit):
    path = out / "snapshot.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_kind_is_refused_in_an_older_format_and_an_unknown_kind_is_refused(tmp_path):
    out, _, _ = _collect(tmp_path, _observed(DATA, GOOD_TTL))
    _rewrite(out, lambda d: d.update(snapshot_format_version="1.1"))
    with pytest.raises(SnapshotError, match="unknown record kind"):
        Snapshot(out)
    _rewrite(out, lambda d: d.update(snapshot_format_version="1.2"))
    _rewrite(out, lambda d: d["records"][-1].update(kind="shacl_validation"))
    with pytest.raises(SnapshotError, match="unknown record kind"):
        Snapshot(out)


def test_a_drifted_report_is_refused_like_any_body(tmp_path):
    out, _, _ = _collect(tmp_path, _observed(DATA, GOOD_TTL))
    (out / "bodies" / "study-rdf-validation.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SnapshotError, match="no longer matches"):
        Snapshot(out)


def test_the_server_lists_and_reads_the_validation_record(ingested, tmp_path):
    out, _, _ = _collect(tmp_path, _observed(DATA, GOOD_TTL))
    service = DatasetService(ingested.output_dir, mode="raw", evidence_snapshot=out)
    listed = {r["id"]: r for r in service.list_snapshot_records()["records"]}
    assert listed["study-rdf-validation"]["result"] == "conforms"
    text = service.read_snapshot_record("study-rdf-validation")["text"]
    assert json.loads(text)["validated_record"] == "study-rdf"


def test_cli_prints_the_validation_result(monkeypatch, tmp_path, capsys):
    responses = {DATA: _observed(DATA, UNDEFINED_TTL), VOCAB: _observed(VOCAB, VOCAB_TTL)}

    def fetch(url, *, accept, max_body):
        return dict(responses[url])

    monkeypatch.setattr("data2agent.profiles.fair.live.fetch_public_document", fetch)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(_spec()), encoding="utf-8")
    assert main(["snapshot", str(spec), "-o", str(tmp_path / "s")]) == 0
    out = capsys.readouterr().out
    assert "study-rdf-validation" in out and "does_not_conform" in out
