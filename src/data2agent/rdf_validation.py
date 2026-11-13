"""Offline validation of RDF captured in an evidence snapshot.

A FAIR I1 check asks whether a formal representation has been validated. The
snapshot collector can answer that for RDF it has itself captured: it parses
the frozen body and checks every term the data takes from a declared
vocabulary namespace against the frozen vocabulary document. Nothing is
fetched here; only bodies already written to the snapshot are read.

Supported formats are Turtle, RDF/XML and N-Triples. JSON-LD is refused on
purpose: parsing it can fetch remote contexts, which would break the offline
guarantee. RDF/XML that declares entities is refused before parsing.

The report states what was checked and what was found; it never decides an
I1 label. ``not_validated`` means the input was missing, cut or unusable, and
is never a finding against the data.
"""

from __future__ import annotations

from typing import Any

METHOD = "d2a-rdf-validation-1"
# Formats a validated document may use (parsed by rdflib).
FORMATS = {"turtle": "turtle", "rdfxml": "xml", "ntriples": "nt"}
# A vocabulary may also be OWL/XML, a W3C OWL 2 syntax that is not RDF: some
# ontologies are published only that way. Only its declarations are read.
VOCABULARY_FORMATS = (*FORMATS, "owlxml")
MAX_LISTED_TERMS = 200
_OWL = "{http://www.w3.org/2002/07/owl#}"
_XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
_OWLXML_ENTITIES = (
    "Class",
    "ObjectProperty",
    "DataProperty",
    "AnnotationProperty",
    "NamedIndividual",
    "Datatype",
)

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
# A vocabulary defines a term by declaring it with one of these types.
_DECLARATION_TYPES = frozenset(
    {
        "http://www.w3.org/2002/07/owl#Class",
        "http://www.w3.org/2002/07/owl#ObjectProperty",
        "http://www.w3.org/2002/07/owl#DatatypeProperty",
        "http://www.w3.org/2002/07/owl#AnnotationProperty",
        "http://www.w3.org/2002/07/owl#NamedIndividual",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property",
        "http://www.w3.org/2000/01/rdf-schema#Class",
        "http://www.w3.org/2000/01/rdf-schema#Datatype",
    }
)


class RDFValidationUnavailable(RuntimeError):
    """rdflib is not installed, so no validation can be run."""


def require_rdflib() -> str:
    """Return rdflib's version, or explain how to install it."""
    try:
        import rdflib
    except ImportError:
        raise RDFValidationUnavailable(
            "RDF validation needs rdflib: install data2agent[rdf-validation]"
        ) from None
    return str(rdflib.__version__)


ENTITIES_REFUSED = "RDF/XML declaring entities is not parsed (entity expansion refused)"


def _document_base(record: dict[str, Any]) -> str | None:
    """The URL the body was served from: the base for its relative IRIs."""
    return record.get("final_url") or record.get("url")


def _parse(body: bytes, fmt: str, base: str | None) -> tuple[Any, str | None]:
    import rdflib

    if fmt == "rdfxml" and b"<!ENTITY" in body:
        return None, ENTITIES_REFUSED
    graph = rdflib.Graph()
    try:
        # Without the document's URL as base, relative IRIs (rdf:about="#X")
        # would resolve against nothing and no longer match their namespace.
        graph.parse(data=body, format=FORMATS[fmt], publicID=base)
    except Exception as error:  # any parser failure is a finding, never a crash
        return None, f"{type(error).__name__}: {str(error)[:300]}"
    return graph, None


def _defined_terms(graph: Any, namespace: str) -> set[str]:
    import rdflib

    rdf_type = rdflib.URIRef(_RDF_TYPE)
    return {
        str(subject)
        for subject, kind in graph.subject_objects(rdf_type)
        if str(kind) in _DECLARATION_TYPES and str(subject).startswith(namespace)
    }


def _owlxml_declared_terms(
    body: bytes, namespace: str, document_base: str | None
) -> tuple[set[str] | None, str | None]:
    """Terms declared in an OWL/XML document (``<Declaration>`` elements), standard library only."""
    import xml.etree.ElementTree as ElementTree
    from urllib.parse import urljoin

    if b"<!ENTITY" in body:
        return None, ENTITIES_REFUSED
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        return None, f"ParseError: {str(error)[:300]}"
    if root.tag != f"{_OWL}Ontology":
        return None, "not an OWL/XML document (its root is not owl:Ontology)"
    base = root.get(_XML_BASE) or root.get("ontologyIRI") or document_base or ""
    prefixes = {
        element.get("name", ""): element.get("IRI", "") for element in root.iter(f"{_OWL}Prefix")
    }
    declared = set()
    for declaration in root.iter(f"{_OWL}Declaration"):
        for entity in declaration:
            if entity.tag.removeprefix(_OWL) not in _OWLXML_ENTITIES:
                continue
            if entity.get("IRI") is not None:
                iri = urljoin(base, entity.get("IRI"))
            elif entity.get("abbreviatedIRI") and ":" in entity.get("abbreviatedIRI"):
                prefix, local = entity.get("abbreviatedIRI").split(":", 1)
                if prefix not in prefixes:
                    continue
                iri = prefixes[prefix] + local
            else:
                continue
            if iri.startswith(namespace):
                declared.add(iri)
    return declared, None


def _vocabulary_terms(
    body: bytes, fmt: str, namespace: str, base: str | None
) -> tuple[set[str] | None, str | None]:
    if fmt == "owlxml":
        return _owlxml_declared_terms(body, namespace, base)
    graph, error = _parse(body, fmt, base)
    return (None, error) if graph is None else (_defined_terms(graph, namespace), None)


def _used_terms(graph: Any, namespace: str) -> set[str]:
    """Predicates, and classes given with rdf:type, taken from ``namespace``."""
    used = set()
    for _, predicate, obj in graph:
        if str(predicate).startswith(namespace):
            used.add(str(predicate))
        if str(predicate) == _RDF_TYPE and str(obj).startswith(namespace):
            used.add(str(obj))
    return used


def _input_problem(record: dict[str, Any] | None, body: bytes | None, role: str) -> str | None:
    if record is None or body is None:
        return f"{role} has no body in this snapshot"
    if record.get("result") != "reached":
        # An HTTP error page is captured too, but it is not the document.
        return (
            f"{role} was not reached (result {record.get('result')!r}, "
            f"HTTP status {record.get('status_code')}); an error response is not the document"
        )
    if record.get("truncated"):
        reason = record.get("truncation_reason")
        return f"{role} is truncated ({reason}); a partial document is not validated"
    return None


def validate(
    data: tuple[dict[str, Any] | None, bytes | None, str],
    vocabularies: list[tuple[dict[str, Any] | None, bytes | None, str, str]],
    *,
    rdflib_version: str,
) -> dict[str, Any]:
    """Validate one captured RDF document against captured vocabularies.

    ``data`` is (record, body, format); each vocabulary is (record, body,
    format, namespace). Records are the snapshot records the bodies belong to.
    """
    record, body, fmt = data
    report: dict[str, Any] = {
        "method": METHOD,
        "validator": {"tool": "rdflib", "version": rdflib_version},
        "validated_record": record.get("id") if record else None,
        "validated_body_sha256": record.get("body_sha256") if record else None,
        "format": fmt,
        "base_iri": _document_base(record) if record else None,
        "syntax": None,
        "vocabularies": [],
        "result": "not_validated",
        "reasons": [],
    }
    problem = _input_problem(record, body, "the validated record")
    if problem:
        report["reasons"].append(problem)
        return report
    graph, error = _parse(body, fmt, report["base_iri"])
    if error == ENTITIES_REFUSED:
        # Refusing to parse is our choice, not a defect of the document.
        report["reasons"].append(error)
        return report
    report["syntax"] = {
        "parsed": graph is not None,
        "error": error,
        "triples": len(graph) if graph is not None else None,
    }
    if graph is None:
        report["result"] = "does_not_conform"
        report["reasons"].append(f"the document does not parse as {fmt}")
        return report

    unusable = False
    undefined_total = 0
    for vocab_record, vocab_body, vocab_fmt, namespace in vocabularies:
        entry: dict[str, Any] = {
            "record": vocab_record.get("id") if vocab_record else None,
            "body_sha256": vocab_record.get("body_sha256") if vocab_record else None,
            "format": vocab_fmt,
            "namespace": namespace,
        }
        problem = _input_problem(vocab_record, vocab_body, f"vocabulary {entry['record']!r}")
        defined, vocab_error = (
            (None, problem)
            if problem
            else _vocabulary_terms(vocab_body, vocab_fmt, namespace, _document_base(vocab_record))
        )
        if defined is None:
            entry.update(checked=False, error=vocab_error)
            report["reasons"].append(f"vocabulary {entry['record']!r} unusable: {vocab_error}")
            unusable = True
            report["vocabularies"].append(entry)
            continue
        used = _used_terms(graph, namespace)
        undefined = sorted(used - defined)
        undefined_total += len(undefined)
        entry.update(
            checked=True,
            error=None,
            terms_defined=len(defined),
            terms_used=len(used),
            undefined_count=len(undefined),
            undefined_terms=undefined[:MAX_LISTED_TERMS],
        )
        if not defined:
            entry["note"] = "the vocabulary declares no term in this namespace"
        report["vocabularies"].append(entry)

    if unusable:
        report["result"] = "not_validated"
    elif undefined_total:
        report["result"] = "does_not_conform"
        report["reasons"].append(
            f"{undefined_total} term(s) used from a declared namespace "
            "are not defined in its vocabulary"
        )
    else:
        report["result"] = "conforms"
    return report
