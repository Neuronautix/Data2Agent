"""Compile a ``d2a-ontology-snapshot-1`` from pinned ontology artifacts (offline, build time only).

The runtime (:mod:`constraints`) reads JSON and needs nothing but the standard
library. Building a snapshot from RDF needs ``rdflib`` (``pip install
'data2agent[semantic-build]'``); building one from JSON term lists does not. The
build spec (``d2a-snapshot-build-1``) lists each artifact with the SHA-256 of its
exact bytes, its origin and its status; an artifact whose bytes differ is refused,
so a snapshot can be rebuilt byte-for-byte and audited.

Like :mod:`constraints`, this module imports nothing else from ``data2agent`` and
is vendored byte-identically where needed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .constraints import SNAPSHOT_SCHEMA, TERM_KINDS, OntologySnapshot, SemanticError

BUILD_SCHEMA = "d2a-snapshot-build-1"
TERMS_JSON_FORMAT = "d2a-terms-json-1"
RDF_FORMATS = ("turtle", "xml", "json-ld", "nt")

_RDFS = "http://www.w3.org/2000/01/rdf-schema#"
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_OWL = "http://www.w3.org/2002/07/owl#"
_SKOS = "http://www.w3.org/2004/02/skos/core#"
_OBO = "http://purl.obolibrary.org/obo/"
_OIO = "http://www.geneontology.org/formats/oboInOwl#"
_DCT = "http://purl.org/dc/terms/"
KIND_BY_TYPE = {
    _OWL + "Class": "class",
    _RDFS + "Class": "class",
    _OWL + "ObjectProperty": "property",
    _OWL + "DatatypeProperty": "property",
    _OWL + "AnnotationProperty": "property",
    _RDF + "Property": "property",
    _SKOS + "Concept": "concept",
    _OWL + "NamedIndividual": "individual",
}
LABELS = (_SKOS + "prefLabel", _RDFS + "label")
SYNONYMS = (_SKOS + "altLabel", _OIO + "hasExactSynonym")
DEFINITIONS = (_SKOS + "definition", _OBO + "IAO_0000115", _RDFS + "comment")
PARENTS = (_RDFS + "subClassOf", _RDFS + "subPropertyOf", _SKOS + "broader")
REPLACED_BY = (_OBO + "IAO_0100001", _DCT + "isReplacedBy")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _english(values: list) -> list[str]:
    """Literals preferring English or untagged, in a stable order."""
    preferred = [v for v in values if getattr(v, "language", None) in (None, "en")]
    return sorted({str(v) for v in (preferred or values)})


def _terms_from_rdf(path: Path, fmt: str, ontology: str, include: Any) -> list[dict]:
    try:
        from rdflib import Graph, Literal, URIRef
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise SemanticError(
            "compiling an RDF artifact needs rdflib: pip install 'data2agent[semantic-build]'"
        ) from error
    graph = Graph().parse(str(path), format=fmt)
    type_iri = URIRef(_RDF + "type")
    terms = []
    for subject in sorted(
        {s for s in graph.subjects(type_iri, None) if isinstance(s, URIRef)}, key=str
    ):
        kinds = sorted(
            {
                KIND_BY_TYPE[str(o)]
                for o in graph.objects(subject, type_iri)
                if str(o) in KIND_BY_TYPE
            }
        )
        if not kinds:
            continue
        kind = next(k for k in TERM_KINDS if k in kinds)

        def literals(predicates, subject=subject):
            return [
                o
                for p in predicates
                for o in graph.objects(subject, URIRef(p))
                if isinstance(o, Literal)
            ]

        def iris(predicates, subject=subject):
            return sorted(
                {
                    str(o)
                    for p in predicates
                    for o in graph.objects(subject, URIRef(p))
                    if isinstance(o, URIRef)
                }
            )

        labels = []
        for predicate in LABELS:
            labels = _english(literals((predicate,)))
            if labels:
                break
        definitions = []
        for predicate in DEFINITIONS:
            definitions = _english(literals((predicate,)))
            if definitions:
                break
        deprecated = any(
            str(o).lower() == "true" for o in graph.objects(subject, URIRef(_OWL + "deprecated"))
        )
        replaced = iris(REPLACED_BY)
        terms.append(
            {
                "iri": str(subject),
                "ontology": ontology,
                "kind": kind,
                "label": labels[0] if labels else None,
                "synonyms": sorted(set(_english(literals(SYNONYMS))) | set(labels[1:])),
                "definition": definitions[0] if definitions else None,
                "parents": [p for p in iris(PARENTS) if p != str(subject)],
                "domain": (iris((_RDFS + "domain",)) or [None])[0],
                "range": (iris((_RDFS + "range",)) or [None])[0],
                "deprecated": deprecated,
                "replaced_by": replaced[0] if replaced else None,
            }
        )
    return _filter(terms, include)


def _filter(terms: list[dict], include: Any) -> list[dict]:
    if include in (None, "all"):
        return terms
    wanted = set(include.get("terms", [])) if isinstance(include, dict) else set()
    if not wanted:
        raise SemanticError("include must be 'all' or {\"terms\": [IRIs]}")
    missing = wanted - {t["iri"] for t in terms}
    if missing:
        raise SemanticError(f"artifact does not define the included terms {sorted(missing)}")
    return [t for t in terms if t["iri"] in wanted]


def build_snapshot(spec_path: Path) -> dict:
    """The snapshot a build spec describes; every artifact is hash-checked before it is read."""
    spec_path = Path(spec_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if spec.get("schema_version") != BUILD_SCHEMA:
        raise SemanticError(f"build spec must declare schema_version {BUILD_SCHEMA}")
    base = spec_path.parent
    terms: dict[str, dict] = {}
    sources = []
    for source in spec["sources"]:
        artifact = base / source["artifact"]
        actual = sha256_file(artifact)
        if actual != source["sha256"]:
            raise SemanticError(
                f"{source['artifact']} has sha256 {actual}, not the pinned {source['sha256']}"
            )
        fmt = source["format"]
        if fmt == TERMS_JSON_FORMAT:
            loaded = json.loads(artifact.read_text(encoding="utf-8"))
            found = _filter(
                [{**t, "ontology": source["ontology"]} for t in loaded["terms"]],
                source.get("include"),
            )
        elif fmt in RDF_FORMATS:
            found = _terms_from_rdf(artifact, fmt, source["ontology"], source.get("include"))
        else:
            raise SemanticError(f"unsupported artifact format {fmt!r}")
        for term in found:
            if term["iri"] in terms:
                raise SemanticError(f"term {term['iri']} is defined by two artifacts")
            terms[term["iri"]] = term
        sources.append(
            {
                k: source.get(k)
                for k in (
                    "ontology",
                    "artifact",
                    "format",
                    "sha256",
                    "origin",
                    "version",
                    "status",
                    "license",
                    "note",
                )
            }
            | {"terms": len(found)}
        )
    for term in terms.values():
        external = [p for p in term.get("parents", []) if p not in terms]
        term["parents"] = [p for p in term.get("parents", []) if p in terms]
        if external:
            term["external_parents"] = external
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA,
        "snapshot_id": spec["snapshot_id"],
        "description": spec.get("description"),
        "prefixes": spec.get("prefixes", {}),
        "ontologies": spec["ontologies"],
        "sources": sources,
        "terms": [terms[iri] for iri in sorted(terms)],
    }
    OntologySnapshot(snapshot)  # the compiled result must load
    return snapshot


def write_snapshot(snapshot: dict, destination: Path) -> str:
    Path(destination).write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return OntologySnapshot(snapshot).sha256


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="compile a d2a-ontology-snapshot-1 from a build spec"
    )
    parser.add_argument("spec", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if --out differs from a fresh build"
    )
    args = parser.parse_args(argv)
    snapshot = build_snapshot(args.spec)
    if args.check:
        current = json.loads(args.out.read_text(encoding="utf-8")) if args.out.is_file() else None
        if current != snapshot:
            print(f"{args.out} is stale: rebuild it from {args.spec}")
            return 1
        print(f"{args.out} matches {args.spec} (sha256 {OntologySnapshot(snapshot).sha256})")
        return 0
    print(write_snapshot(snapshot, args.out))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
