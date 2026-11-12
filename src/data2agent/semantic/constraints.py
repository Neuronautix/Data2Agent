"""Runtime ontology constraints over a pinned, local ontology snapshot.

This module is the semantic constraint interface shared by Data2Agent's optional
semantic layer and by the SSIC pilot (which vendors a byte-identical copy until
its Data2Agent pin includes this package). It is standard-library only and imports
nothing else from ``data2agent``, so it can be copied verbatim and its SHA-256
compared.

Two versioned inputs, both plain JSON:

``d2a-ontology-snapshot-1``
    The authorised terms, compiled offline from pinned ontology artifacts whose
    SHA-256 and origin the snapshot records. Each term has a canonical IRI, a
    label, synonyms, a definition, its ontology, its kind, its parents (subclass
    or SKOS broader) and optional domain/range. The snapshot is identified by the
    SHA-256 of its canonical JSON.

``d2a-mapping-policy-1``
    Field admissibility, which OWL alone does not give (OWL is open-world: it
    says what a term means, not which terms a given field may take). Each field
    names whole ontologies, branches (a root IRI and everything below it) and/or
    explicit term sets, minus exclusions, plus a datatype, a term kind and a
    cardinality. A policy is bound to one snapshot by SHA-256.

The four operations:

- :meth:`OntologyRuntime.get_mapping_constraints` resolves a task profile into
  per-field constraints with the admissible term set;
- :meth:`OntologyRuntime.search_ontology_terms` retrieves candidate terms inside
  that scope only, ranked by a fixed, documented lexical rule;
- :meth:`OntologyRuntime.get_term_context` returns hierarchy, domain/range and
  version information for one IRI;
- :meth:`OntologyRuntime.validate_semantic_mapping` checks a mapping decision
  against the constraints, independently of whoever produced it.

Validation is *formal*: it establishes that a term exists in the pinned snapshot,
is written as a canonical IRI, lies inside the field's authorised scope and fits
the declared datatype and cardinality. It does not establish that the mapping is
scientifically correct; every validation result says so. Retrieval never commits
to a term: it returns candidates, and an empty result is a valid answer that the
caller turns into ``unresolved``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA = "d2a-ontology-snapshot-1"
SNAPSHOT_SCHEMA_2 = "d2a-ontology-snapshot-2"
POLICY_SCHEMA_2 = "d2a-mapping-policy-2"
POLICY_SCHEMA = "d2a-mapping-policy-1"
CONSTRAINTS_CONTRACT = "d2a-mapping-constraints-1"
VALIDATION_CONTRACT = "d2a-semantic-validation-1"
ENGINE_VERSION = "1.0.0"

DECISIONS = ("map", "abstain", "needs_review")
TERM_KINDS = ("class", "property", "concept", "individual")
LITERAL_DATATYPES = (
    "literal",
    "xsd:string",
    "xsd:boolean",
    "xsd:decimal",
    "xsd:integer",
    "xsd:date",
    "xsd:dateTime",
)
# Lexical spaces of the supported XSD datatypes (XML Schema 1.1 Part 2), checked before the value
# space. Supported subset, by design: no leap seconds, no hour 24:00:00, years 0001-9999.
_XSD_INTEGER = re.compile(r"^[+-]?[0-9]+$")
_XSD_DECIMAL = re.compile(r"^[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)$")
_XSD_TZ = r"(Z|[+-](0[0-9]|1[0-3]):[0-5][0-9]|[+-]14:00)?"
_XSD_DATE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})" + _XSD_TZ + "$")
_XSD_DATETIME = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]+)?" + _XSD_TZ + "$"
)
FORMAL_ONLY_NOTE = (
    "formal constraint validity only: the term exists in the pinned snapshot, is a canonical IRI, "
    "lies in the authorised scope and fits datatype and cardinality; this is not evidence that the "
    "mapping is scientifically correct"
)
MAX_TOP_K = 50

# Fixed ranking rule of search_ontology_terms (documented, never tuned per run).
SCORE_EXACT_LABEL = 1.0
SCORE_EXACT_SYNONYM = 0.95
SCORE_TOKEN_WEIGHT = 0.8
SCORE_DEFINITION_WEIGHT = 0.15

_IRI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"{}|\\^`]+$")
_CURIE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):([^\s/][^\s]*)?$")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN = re.compile(r"[a-z0-9]+")


class SemanticError(ValueError):
    """A semantic input is malformed or inconsistent; nothing is guessed."""


class VocabularyUnavailable(SemanticError):
    """The pinned snapshot or policy is missing, unreadable or not the pinned bytes.

    Callers turn this into an explicit ``unresolved``/``needs_review`` outcome;
    it is never silently replaced by another vocabulary or a live service.
    """


def canonical_json(value: Any) -> bytes:
    """The bytes every digest in this module is computed over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def tokens(text: str | None) -> list[str]:
    """Lower-case alphanumeric tokens, camelCase split: ``samplingRate_hz`` -> sampling rate hz."""
    if not text:
        return []
    return _TOKEN.findall(_CAMEL.sub(" ", str(text)).lower())


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise VocabularyUnavailable(f"{what} {path} does not exist") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VocabularyUnavailable(f"{what} {path} is unreadable: {error}") from None


class OntologySnapshot:
    """A verified ``d2a-ontology-snapshot-1`` document."""

    def __init__(self, data: dict, *, expected_sha256: str | None = None):
        if not isinstance(data, dict) or data.get("schema_version") not in (
            SNAPSHOT_SCHEMA,
            SNAPSHOT_SCHEMA_2,
        ):
            raise SemanticError(f"ontology snapshot must declare schema_version {SNAPSHOT_SCHEMA}")
        self.data = data
        self.schema_version = data["schema_version"]
        self.sha256 = sha256_of(data)
        if expected_sha256 is not None and expected_sha256 != self.sha256:
            raise VocabularyUnavailable(
                f"ontology snapshot digest {self.sha256} is not the pinned {expected_sha256}"
            )
        self.snapshot_id = _require_str(data, "snapshot_id", "snapshot")
        self.prefixes: dict[str, str] = dict(data.get("prefixes") or {})
        self.ontologies: dict[str, dict] = dict(data.get("ontologies") or {})
        sources = data.get("sources")
        if not isinstance(sources, list) or not sources:
            raise SemanticError("snapshot must list the pinned sources it was compiled from")
        for source in sources:
            if not isinstance(source, dict) or not re.fullmatch(
                r"[0-9a-f]{64}", str(source.get("sha256"))
            ):
                raise SemanticError("every snapshot source needs a sha256 of the pinned artifact")
        if self.schema_version == SNAPSHOT_SCHEMA and (
            "schemes" in data
            or any("schemes" in t for t in data.get("terms", []) if isinstance(t, dict))
        ):
            raise SemanticError("scheme membership requires snapshot 2")
        self.schemes = (
            set(data.get("schemes", [])) if self.schema_version == SNAPSHOT_SCHEMA_2 else set()
        )
        if any(not isinstance(x, str) or not _IRI.match(x) for x in self.schemes):
            raise SemanticError("scheme identities must be absolute IRIs")
        self.terms: dict[str, dict] = {}
        for term in data.get("terms") or []:
            iri = term.get("iri") if isinstance(term, dict) else None
            if not isinstance(iri, str) or not _IRI.match(iri):
                raise SemanticError(f"snapshot term IRI must be an absolute IRI, got {iri!r}")
            if iri in self.terms:
                raise SemanticError(f"duplicate term {iri} in snapshot")
            if term.get("ontology") not in self.ontologies:
                raise SemanticError(f"term {iri} names an ontology the snapshot does not declare")
            if term.get("kind") not in TERM_KINDS:
                raise SemanticError(
                    f"term {iri} has kind {term.get('kind')!r}, not one of {TERM_KINDS}"
                )
            if self.schema_version == SNAPSHOT_SCHEMA_2:
                memberships = term.get("schemes", [])
                if not isinstance(memberships, list) or any(
                    x not in self.schemes for x in memberships
                ):
                    raise SemanticError(f"term {iri} names an unknown scheme")
                if memberships and term["kind"] != "concept":
                    raise SemanticError(f"non-concept {iri} has SKOS scheme membership")
            self.terms[iri] = term
        if not self.terms:
            raise SemanticError("snapshot holds no terms")
        for iri, term in self.terms.items():
            for parent in term.get("parents") or []:
                if parent not in self.terms:
                    raise SemanticError(f"term {iri} names parent {parent} outside the snapshot")
                if (
                    self.schema_version == SNAPSHOT_SCHEMA_2
                    and self.terms[parent]["kind"] != term["kind"]
                ):
                    raise SemanticError(f"term {iri} has a parent of another kind")
        self._children: dict[str, list[str]] = {}
        for iri, term in sorted(self.terms.items()):
            for parent in term.get("parents") or []:
                self._children.setdefault(parent, []).append(iri)
        self._ancestors: dict[str, frozenset[str]] = {}

    @classmethod
    def load(cls, path: Path, *, expected_sha256: str | None = None) -> OntologySnapshot:
        return cls(_read_json(path, "ontology snapshot"), expected_sha256=expected_sha256)

    def identity(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "sha256": self.sha256,
            "sources": [
                {k: s.get(k) for k in ("ontology", "version", "sha256", "origin", "status")}
                for s in self.data["sources"]
            ],
        }

    def expand(self, value: str) -> str | None:
        """An absolute IRI for ``value`` (absolute, or a CURIE of a declared prefix), else None."""
        if _IRI.match(value):
            return value
        match = _CURIE.match(value)
        if match and match.group(1) in self.prefixes:
            return self.prefixes[match.group(1)] + (match.group(2) or "")
        return None

    def compact(self, iri: str) -> str:
        best = None
        for prefix, base in self.prefixes.items():
            if iri.startswith(base) and (best is None or len(base) > len(self.prefixes[best])):
                best = prefix
        return f"{best}:{iri[len(self.prefixes[best]) :]}" if best else iri

    def ancestors(self, iri: str) -> frozenset[str]:
        """Every term above ``iri`` (transitive parents), cycle-safe."""
        if iri not in self._ancestors:
            seen: set[str] = set()
            stack = list(self.terms[iri].get("parents") or [])
            while stack:
                parent = stack.pop()
                if parent not in seen:
                    seen.add(parent)
                    stack.extend(self.terms[parent].get("parents") or [])
            seen.discard(iri)
            self._ancestors[iri] = frozenset(seen)
        return self._ancestors[iri]

    def children(self, iri: str) -> list[str]:
        return list(self._children.get(iri, []))

    def version_of(self, iri: str) -> dict:
        ontology = self.terms[iri]["ontology"]
        declared = self.ontologies[ontology]
        return {
            "ontology": ontology,
            "ontology_iri": declared.get("iri"),
            "version": declared.get("version"),
            "version_iri": declared.get("version_iri"),
        }


class MappingPolicy:
    """A verified ``d2a-mapping-policy-1`` document bound to one snapshot."""

    def __init__(self, data: dict, snapshot: OntologySnapshot):
        if not isinstance(data, dict) or data.get("schema_version") not in (
            POLICY_SCHEMA,
            POLICY_SCHEMA_2,
        ):
            raise SemanticError(f"mapping policy must declare schema_version {POLICY_SCHEMA}")
        self.data = data
        self.schema_version = data["schema_version"]
        if self.schema_version == POLICY_SCHEMA_2 and snapshot.schema_version != SNAPSHOT_SCHEMA_2:
            raise SemanticError("policy 2 requires snapshot 2")
        self.sha256 = sha256_of(data)
        self.policy_id = _require_str(data, "policy_id", "policy")
        self.version = _require_str(data, "version", "policy")
        binding = data.get("snapshot") or {}
        if (
            binding.get("sha256") != snapshot.sha256
            or binding.get("snapshot_id") != snapshot.snapshot_id
        ):
            raise VocabularyUnavailable(
                f"policy {self.policy_id} is bound to snapshot {binding.get('snapshot_id')}@"
                f"{str(binding.get('sha256'))[:12]}, not to the loaded "
                f"{snapshot.snapshot_id}@{snapshot.sha256[:12]}"
            )
        if data.get("closed_world") is not True:
            raise SemanticError(
                "a mapping policy must be closed_world: true (only listed terms are admissible)"
            )
        fields = data.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise SemanticError("mapping policy needs at least one field")
        self.fields: dict[str, dict] = {}
        for field_id, spec in fields.items():
            self.fields[field_id] = _check_field(field_id, spec, snapshot, self.schema_version)

    @classmethod
    def load(cls, path: Path, snapshot: OntologySnapshot) -> MappingPolicy:
        return cls(_read_json(path, "mapping policy"), snapshot)

    def identity(self) -> dict:
        return {"policy_id": self.policy_id, "version": self.version, "sha256": self.sha256}


def _require_str(data: dict, key: str, what: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise SemanticError(f"{what} needs a non-empty {key}")
    return value


def _check_field(
    field_id: str, spec: Any, snapshot: OntologySnapshot, policy_schema: str = POLICY_SCHEMA
) -> dict:
    if not isinstance(spec, dict):
        raise SemanticError(f"policy field {field_id} must be an object")
    allowed = spec.get("allowed") or {}
    if not isinstance(allowed, dict) or set(allowed) - {
        "ontologies",
        "branches",
        "terms",
        "values",
        *({"schemes"} if policy_schema == POLICY_SCHEMA_2 else set()),
    }:
        raise SemanticError(
            f"policy field {field_id}: allowed takes ontologies, branches, terms and values only"
        )
    exclude = spec.get("exclude") or {}
    if not isinstance(exclude, dict) or set(exclude) - {"branches", "terms"}:
        raise SemanticError(f"policy field {field_id}: exclude takes branches and terms only")
    datatype = spec.get("datatype", "iri")
    if datatype != "iri" and datatype not in LITERAL_DATATYPES:
        raise SemanticError(
            f"policy field {field_id}: datatype {datatype!r} is neither iri nor a literal datatype"
        )
    if datatype == "iri" and allowed.get("values"):
        raise SemanticError(f"policy field {field_id}: literal values need a literal datatype")
    for value in allowed.get("values", []):
        problem = literal_error(datatype, value)
        if problem:
            raise SemanticError(f"policy field {field_id}: allowed value {value!r} {problem}")
    if datatype != "iri" and set(allowed) - {"values"}:
        raise SemanticError(f"policy field {field_id}: a literal field admits values, not terms")
    for scheme in allowed.get("schemes", []):
        if scheme not in snapshot.schemes:
            raise SemanticError(f"policy field {field_id}: unknown scheme {scheme!r}")
    if allowed.get("schemes") and spec.get("term_kinds") != ["concept"]:
        raise SemanticError(f"policy field {field_id}: scheme scope requires concept kind")
    for ontology in allowed.get("ontologies", []):
        if ontology not in snapshot.ontologies:
            raise SemanticError(
                f"policy field {field_id} allows ontology {ontology!r} the snapshot does not hold"
            )
    resolved: dict[str, list[str]] = {}
    for group, source in (
        ("branches", allowed),
        ("terms", allowed),
        ("exclude_branches", exclude),
        ("exclude_terms", exclude),
    ):
        key = group.removeprefix("exclude_")
        iris = []
        for value in source.get(key, []):
            iri = snapshot.expand(value) if isinstance(value, str) else None
            if iri is None or iri not in snapshot.terms:
                raise SemanticError(
                    f"policy field {field_id} names {value!r} in {group}, "
                    "which is not in the snapshot"
                )
            iris.append(iri)
        resolved[group] = iris
    kinds = spec.get("term_kinds") or []
    if any(kind not in TERM_KINDS for kind in kinds):
        raise SemanticError(f"policy field {field_id}: term_kinds must be among {TERM_KINDS}")
    cardinality = spec.get("cardinality") or {"min": 0, "max": 1}
    low, high = cardinality.get("min", 0), cardinality.get("max", 1)
    if (
        not isinstance(low, int)
        or low < 0
        or (high is not None and (not isinstance(high, int) or high < max(low, 1)))
    ):
        raise SemanticError(
            f"policy field {field_id}: cardinality needs 0 <= min <= max (max null = unbounded)"
        )
    return {
        "field": field_id,
        "description": spec.get("description"),
        "datatype": datatype,
        "term_kinds": list(kinds),
        "cardinality": {"min": low, "max": high},
        "allowed": {
            "ontologies": list(allowed.get("ontologies", [])),
            "branches": resolved["branches"],
            "terms": resolved["terms"],
            "values": list(allowed.get("values", [])),
            **(
                {"schemes": list(allowed.get("schemes", []))}
                if policy_schema == POLICY_SCHEMA_2
                else {}
            ),
        },
        "exclude": {"branches": resolved["exclude_branches"], "terms": resolved["exclude_terms"]},
        "allow_deprecated": bool(spec.get("allow_deprecated", False)),
        "unit": spec.get("unit"),
        "notes": spec.get("notes"),
    }


class OntologyRuntime:
    """The four operations over one verified snapshot and one policy bound to it."""

    def __init__(self, snapshot: OntologySnapshot, policy: MappingPolicy):
        self.snapshot, self.policy = snapshot, policy
        self._scope_cache: dict[str, tuple[str, ...]] = {}
        self.calls: list[
            dict
        ] = []  # a tool-call log for efficiency accounting; never consulted for decisions

    @classmethod
    def load(
        cls, snapshot_path: Path, policy_path: Path, *, expected_snapshot_sha256: str | None = None
    ) -> OntologyRuntime:
        snapshot = OntologySnapshot.load(snapshot_path, expected_sha256=expected_snapshot_sha256)
        return cls(snapshot, MappingPolicy.load(policy_path, snapshot))

    def identity(self) -> dict:
        return {
            "engine_version": ENGINE_VERSION,
            "snapshot": self.snapshot.identity(),
            "policy": self.policy.identity(),
        }

    # -- scope ---------------------------------------------------------------------------------

    def _in_scope(self, iri: str, field: dict) -> tuple[bool, str]:
        term = self.snapshot.terms.get(iri)
        if term is None:
            return False, "unknown_iri"
        if term.get("deprecated") and not field["allow_deprecated"]:
            return False, "deprecated_term"
        if field["term_kinds"] and term["kind"] not in field["term_kinds"]:
            return False, "kind_mismatch"
        lineage = self.snapshot.ancestors(iri) | {iri}
        if iri in field["exclude"]["terms"] or lineage & set(field["exclude"]["branches"]):
            return False, "excluded"
        allowed = field["allowed"]
        schemes = allowed.get("schemes", [])
        if schemes and not set(term.get("schemes", [])) & set(schemes):
            return False, "outside_scope"
        if (
            (schemes and not any(allowed[k] for k in ("ontologies", "branches", "terms")))
            or term["ontology"] in allowed["ontologies"]
            or iri in allowed["terms"]
            or lineage & set(allowed["branches"])
        ):
            return True, "admissible"
        return False, "outside_scope"

    def admissible_terms(self, field_id: str) -> tuple[str, ...]:
        if field_id not in self._scope_cache:
            field = self._field(field_id)
            self._scope_cache[field_id] = tuple(
                iri for iri in sorted(self.snapshot.terms) if self._in_scope(iri, field)[0]
            )
        return self._scope_cache[field_id]

    def _field(self, field_id: str) -> dict:
        if field_id not in self.policy.fields:
            raise SemanticError(
                f"field {field_id!r} is not declared by policy {self.policy.policy_id}"
            )
        return self.policy.fields[field_id]

    # -- 1. constraints ------------------------------------------------------------------------

    def get_mapping_constraints(self, task_profile: dict | None = None) -> dict:
        """Per-field constraints for a task profile ``{"fields": [ids]}`` (all fields when omitted).

        Each field carries its datatype, term kinds, cardinality, the authorised scope as declared
        (ontologies, branches, explicit terms, literal values, exclusions) and the resolved
        admissible term IRIs with their labels.
        """
        self.calls.append({"tool": "get_mapping_constraints"})
        wanted = (task_profile or {}).get("fields") or sorted(self.policy.fields)
        fields = {}
        for field_id in wanted:
            field = self._field(field_id)
            admissible = self.admissible_terms(field_id)
            fields[field_id] = {
                **field,
                "admissible_terms": [
                    {
                        "iri": iri,
                        "curie": self.snapshot.compact(iri),
                        "label": self.snapshot.terms[iri].get("label"),
                    }
                    for iri in admissible
                ],
                "admissible_count": len(admissible)
                if field["datatype"] == "iri"
                else len(field["allowed"]["values"]),
            }
        return {
            "contract": CONSTRAINTS_CONTRACT,
            **self.identity(),
            "closed_world": True,
            "fields": fields,
        }

    # -- 2. search -----------------------------------------------------------------------------

    def search_ontology_terms(self, query: str, constraints: dict | str, top_k: int = 5) -> dict:
        """Candidate terms for ``query`` inside one field's authorised scope, best first.

        ``constraints`` is a field id or one field entry of :meth:`get_mapping_constraints`.
        Ranking: exact label 1.0, exact synonym 0.95, otherwise 0.8 x Jaccard overlap of tokens
        between the query and the label plus synonyms; plus 0.15 x the share of query tokens in the
        definition. Ties break by IRI. Terms outside the scope are never returned; how many would
        have matched is reported as ``out_of_scope_matches``. An empty ``candidates`` list is a
        valid result meaning no authorised term matches.
        """
        field_id = constraints if isinstance(constraints, str) else constraints.get("field")
        field = self._field(field_id)
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= MAX_TOP_K:
            raise SemanticError(f"top_k must be an integer within 1..{MAX_TOP_K}")
        self.calls.append(
            {"tool": "search_ontology_terms", "field": field_id, "query": query, "top_k": top_k}
        )
        if field["datatype"] != "iri":
            hits = [
                {
                    "value": value,
                    "score": 1.0 if str(query).strip().lower() == str(value).lower() else 0.0,
                }
                for value in field["allowed"]["values"]
            ]
            return {
                "field": field_id,
                "query": query,
                "top_k": top_k,
                "literal_values": hits,
                "candidates": [],
                "out_of_scope_matches": 0,
                "scope_size": len(field["allowed"]["values"]),
            }
        scored, outside = [], 0
        admissible = set(self.admissible_terms(field_id))
        for iri, term in self.snapshot.terms.items():
            score, reason = _score(query, term)
            if score <= 0:
                continue
            if iri not in admissible:
                outside += 1
                continue
            scored.append((score, iri, reason))
        scored.sort(key=lambda item: (-item[0], item[1]))
        candidates = [
            self._describe(iri) | {"score": round(score, 4), "match": reason}
            for score, iri, reason in scored[:top_k]
        ]
        return {
            "field": field_id,
            "query": query,
            "top_k": top_k,
            "candidates": candidates,
            "out_of_scope_matches": outside,
            "scope_size": len(admissible),
        }

    def _describe(self, iri: str) -> dict:
        term = self.snapshot.terms[iri]
        return {
            "iri": iri,
            "curie": self.snapshot.compact(iri),
            "label": term.get("label"),
            "synonyms": list(term.get("synonyms") or []),
            "definition": term.get("definition"),
            "kind": term["kind"],
            **self.snapshot.version_of(iri),
        }

    # -- 3. context ----------------------------------------------------------------------------

    def get_term_context(self, iri: str) -> dict:
        """Hierarchy, domain/range, relations and version of one term, or ``found: false``."""
        self.calls.append({"tool": "get_term_context", "iri": iri})
        canonical = self.snapshot.expand(iri) if isinstance(iri, str) else None
        if canonical is None or canonical not in self.snapshot.terms:
            return {"iri": iri, "found": False}
        term = self.snapshot.terms[canonical]
        admissible_for = sorted(
            f for f in self.policy.fields if canonical in self.admissible_terms(f)
        )
        return {
            "found": True,
            **self._describe(canonical),
            "canonical_iri": canonical,
            "non_canonical_input": canonical != iri,
            "parents": list(term.get("parents") or []),
            "ancestors": sorted(self.snapshot.ancestors(canonical)),
            "children": self.snapshot.children(canonical),
            "domain": term.get("domain"),
            "range": term.get("range"),
            "relations": dict(term.get("relations") or {}),
            "deprecated": bool(term.get("deprecated")),
            "replaced_by": term.get("replaced_by"),
            "admissible_for_fields": admissible_for,
        }

    # -- 4. validation -------------------------------------------------------------------------

    def validate_semantic_mapping(
        self,
        mapping: dict | list,
        constraints: dict | None = None,
        *,
        shacl: Callable[[list[dict]], dict] | None = None,
    ) -> dict:
        """Check mapping decisions against the policy, independently of whoever produced them.

        A decision is ``{"field": id, "decision": "map"|"abstain"|"needs_review", "value": IRI,
        literal or list, "reason": str}``. ``constraints`` (from :meth:`get_mapping_constraints`)
        limits the fields a decision may address. ``shacl``, when given, receives the decisions
        that passed and returns a report with a boolean ``conforms``; without it SHACL is reported
        as ``not_run``. A decision that abstains or asks for review is formally valid when it gives
        a reason: unresolved is an allowed outcome, never an error.
        """
        self.calls.append({"tool": "validate_semantic_mapping"})
        items = mapping if isinstance(mapping, list) else [mapping]
        allowed_fields = set(constraints["fields"]) if constraints else set(self.policy.fields)
        results = []
        for index, item in enumerate(items):
            results.append(self._validate_one(index, item, allowed_fields))
        passed = [r for r in results if r["status"] == "admissible"]
        shacl_report = {"status": "not_run"}
        if shacl is not None and passed:
            report = shacl([items[r["index"]] for r in passed])
            shacl_report = {
                "status": "run",
                "conforms": bool(report.get("conforms")),
                "report": report,
            }
        formally_valid = all(r["status"] != "invalid" for r in results) and shacl_report.get(
            "conforms", True
        )
        return {
            "contract": VALIDATION_CONTRACT,
            **self.identity(),
            "formally_valid": bool(formally_valid),
            "note": FORMAL_ONLY_NOTE,
            "results": results,
            "shacl": shacl_report,
            "counts": {
                status: sum(r["status"] == status for r in results)
                for status in ("admissible", "invalid", "unresolved")
            },
        }

    def _validate_one(self, index: int, item: Any, allowed_fields: set[str]) -> dict:
        errors: list[dict] = []
        warnings: list[dict] = []
        result = {"index": index, "field": item.get("field") if isinstance(item, dict) else None}

        def finish(status: str, canonical: list | None = None) -> dict:
            return {
                **result,
                "status": status,
                "errors": errors,
                "warnings": warnings,
                "canonical_values": canonical,
            }

        if not isinstance(item, dict):
            errors.append({"code": "malformed", "message": "a mapping decision must be an object"})
            return finish("invalid")
        field_id, decision = item.get("field"), item.get("decision", "map")
        if field_id not in self.policy.fields or field_id not in allowed_fields:
            errors.append(
                {
                    "code": "unknown_field",
                    "message": f"field {field_id!r} is not in the constraints",
                }
            )
            return finish("invalid")
        if decision not in DECISIONS:
            errors.append(
                {"code": "unknown_decision", "message": f"decision must be one of {DECISIONS}"}
            )
            return finish("invalid")
        if decision != "map":
            if item.get("value") not in (None, [], ""):
                errors.append(
                    {
                        "code": "value_with_abstention",
                        "message": "an abstaining or needs_review decision must not carry a value",
                    }
                )
                return finish("invalid")
            if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                errors.append(
                    {"code": "missing_reason", "message": "an unresolved decision must state why"}
                )
                return finish("invalid")
            return finish("unresolved")
        field = self.policy.fields[field_id]
        raw = item.get("value")
        values = raw if isinstance(raw, list) else [raw]
        if any(v is None for v in values):
            # Never filtered away: a null inside a map decision is an error in the payload, and
            # dropping it would let [valid, None] pass a cardinality of one (no silent repair).
            errors.append(
                {
                    "code": "null_value",
                    "value": raw,
                    "message": "a map decision must not contain null values",
                }
            )
            return finish("invalid")
        low, high = field["cardinality"]["min"], field["cardinality"]["max"]
        if len(values) < max(low, 1) or (high is not None and len(values) > high):
            errors.append(
                {
                    "code": "cardinality_violation",
                    "message": f"{len(values)} value(s) for {field_id}; allowed {low}.."
                    f"{high if high is not None else 'n'}",
                }
            )
            return finish("invalid")
        canonical: list = []
        for value in values:
            if field["datatype"] != "iri":
                problem = literal_error(field["datatype"], value)
                if problem:
                    errors.append(
                        {
                            "code": "datatype_mismatch",
                            "value": value,
                            "message": f"{field_id} takes a {field['datatype']} literal: {problem}",
                        }
                    )
                elif field["allowed"]["values"] and value not in field["allowed"]["values"]:
                    errors.append(
                        {
                            "code": "literal_not_allowed",
                            "value": value,
                            "message": f"{value!r} is not in the value set of {field_id}",
                        }
                    )
                canonical.append(value)
                continue
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    {
                        "code": "datatype_mismatch",
                        "value": value,
                        "message": f"{field_id} takes an IRI",
                    }
                )
                continue
            iri = self.snapshot.expand(value)
            if iri is None:
                errors.append(
                    {
                        "code": "label_not_iri",
                        "value": value,
                        "message": "a label or unprefixed name is not an identifier; give the IRI",
                    }
                )
                continue
            if iri != value:
                warnings.append(
                    {
                        "code": "non_canonical_form",
                        "value": value,
                        "canonical": iri,
                        "message": "CURIE expanded with the snapshot's declared prefix",
                    }
                )
            ok, why = self._in_scope(iri, field)
            if not ok:
                message = {
                    "unknown_iri": "not a term of the pinned snapshot",
                    "deprecated_term": "deprecated in the pinned snapshot",
                    "kind_mismatch": f"term kind is not one of {field['term_kinds']}",
                    "excluded": f"explicitly excluded for {field_id}",
                    "outside_scope": f"outside the authorised scope of {field_id}",
                }[why]
                error = {"code": why, "value": value, "canonical": iri, "message": message}
                if why == "deprecated_term":
                    error["replaced_by"] = self.snapshot.terms[iri].get("replaced_by")
                errors.append(error)
            canonical.append(iri)
        return finish("invalid" if errors else "admissible", canonical)


def literal_error(datatype: str, value: Any) -> str | None:
    """Why ``value`` is not in the lexical and value space of ``datatype``, or None when it is.

    Values are never coerced: ``"12"`` is an ``xsd:integer`` lexical form, ``True`` is not an
    integer, ``float('nan')`` is not a decimal, ``"2026-02-30"`` is not a date.
    """
    if isinstance(value, bool):
        return None if datatype == "xsd:boolean" else "a boolean is not of this datatype"
    if datatype == "literal":
        if isinstance(value, str):
            return None
        if isinstance(value, (int, float)):
            return None if math.isfinite(value) else "a non-finite number is not a literal"
        return "a literal must be a string or a finite number"
    if datatype == "xsd:boolean":
        return None if value in ("true", "false", "1", "0") else "not true, false, 1 or 0"
    if datatype == "xsd:integer":
        if isinstance(value, int):
            return None
        return None if isinstance(value, str) and _XSD_INTEGER.match(value) else "not an integer"
    if datatype == "xsd:decimal":
        if isinstance(value, int):
            return None
        if isinstance(value, float):
            return None if math.isfinite(value) else "a non-finite number is not a decimal"
        return None if isinstance(value, str) and _XSD_DECIMAL.match(value) else "not a decimal"
    if not isinstance(value, str):
        return "must be a string"
    if datatype == "xsd:string":
        return None
    if datatype == "xsd:date":
        match = _XSD_DATE.match(value)
        if not match:
            return "not an xsd:date (YYYY-MM-DD with an optional zone)"
        try:
            date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            return "not a calendar date"
        return None
    if datatype == "xsd:dateTime":
        match = _XSD_DATETIME.match(value)
        if not match:
            return "not an xsd:dateTime (YYYY-MM-DDThh:mm:ss with optional fraction and zone)"
        try:
            datetime(*(int(match[i]) for i in range(1, 7)))
        except ValueError:
            return "not a valid date and time"
        return None
    return f"unsupported datatype {datatype}"


def _score(query: str, term: dict) -> tuple[float, str]:
    """The fixed ranking rule (see :meth:`OntologyRuntime.search_ontology_terms`)."""
    text = str(query or "").strip().lower()
    if not text:
        return 0.0, "empty"
    label = str(term.get("label") or "").lower()
    synonyms = [str(s).lower() for s in term.get("synonyms") or []]
    if text == label:
        return SCORE_EXACT_LABEL, "exact_label"
    if text in synonyms:
        return SCORE_EXACT_SYNONYM, "exact_synonym"
    query_tokens = set(tokens(query))
    if not query_tokens:
        return 0.0, "empty"
    names = set(tokens(label)).union(*(set(tokens(s)) for s in synonyms))
    union = query_tokens | names
    lexical = SCORE_TOKEN_WEIGHT * len(query_tokens & names) / len(union) if union else 0.0
    definition = set(tokens(term.get("definition")))
    bonus = SCORE_DEFINITION_WEIGHT * len(query_tokens & definition) / len(query_tokens)
    score = lexical + bonus
    return score, ("token_overlap" if lexical else "definition_overlap") if score else "none"


# Module-level forms of the four operations, for callers that hold a runtime explicitly.


def get_mapping_constraints(task_profile: dict | None, *, runtime: OntologyRuntime) -> dict:
    return runtime.get_mapping_constraints(task_profile)


def search_ontology_terms(
    query: str, constraints: dict | str, top_k: int = 5, *, runtime: OntologyRuntime
) -> dict:
    return runtime.search_ontology_terms(query, constraints, top_k)


def get_term_context(iri: str, *, runtime: OntologyRuntime) -> dict:
    return runtime.get_term_context(iri)


def validate_semantic_mapping(
    mapping: dict | list, constraints: dict | None = None, *, runtime: OntologyRuntime, shacl=None
) -> dict:
    return runtime.validate_semantic_mapping(mapping, constraints, shacl=shacl)


def unavailable_outcome(field_ids: Iterable[str], error: Exception) -> list[dict]:
    """Outcome when the vocabulary cannot be loaded: every field needs review, nothing guessed."""
    return [
        {
            "field": field_id,
            "decision": "needs_review",
            "value": None,
            "reason": f"vocabulary unavailable: {error}",
        }
        for field_id in field_ids
    ]
