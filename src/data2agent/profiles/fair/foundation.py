"""GO FAIR Foundation interpretations as an evidence-aware assessment surface."""

# ruff: noqa: E501 -- keep each source-grounded criterion as one readable statement.

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ...errors import LookupRejected, echo


def _item(
    identifier: str,
    question: str,
    required_evidence: tuple[str, ...],
    next_action: str,
    *,
    local_rules: tuple[str, ...] = (),
) -> dict[str, Any]:
    slug = identifier.lower().replace(".", "-")
    return {
        "id": identifier,
        "question": question,
        "source": f"https://www.gofair.foundation/{slug}",
        "required_evidence": list(required_evidence),
        "local_rules": list(local_rules),
        "next_action": next_action,
    }


PRINCIPLES: tuple[dict[str, Any], ...] = (
    _item(
        "F1",
        "Do data and metadata have globally unique, persistent, resolvable identifiers that identify the intended objects?",
        (
            "object-to-identifier mappings for data and metadata",
            "issuer uniqueness and persistence policy",
            "observed resolution to the intended objects",
        ),
        "Assign identifiers through a suitable service, record its persistence policy, and verify what each identifier resolves to.",
        local_rules=("F1-PID-METADATA", "F1-PID-RESOLVABLE"),
    ),
    _item(
        "F2",
        "Is the resource described by rich metadata that supports discovery by attributes?",
        (
            "nonempty citation metadata",
            "descriptive and domain search fields",
            "validation against a selected discovery metadata schema",
        ),
        "Add curator-verified citation, descriptive, and domain search fields, then validate the chosen metadata schema.",
        local_rules=("F2-METADATA-PRESENT",),
    ),
    _item(
        "F3",
        "Does metadata explicitly identify the data it describes through an unambiguous relationship?",
        (
            "known metadata-to-data predicate or schema field",
            "identifier of the described data object",
            "verification that the link denotes this resource",
        ),
        "Add a typed metadata-to-data identifier link and verify that it points to the assessed resource.",
        local_rules=("F3-METADATA-LINKS-DATA",),
    ),
    _item(
        "F4",
        "Are data or metadata indexed in a searchable resource?",
        (
            "registry identity",
            "search by identifier and descriptive attributes",
            "returned record identity and timestamped query",
        ),
        "Index the metadata in a searchable registry and test discovery by identifier and descriptive attributes.",
        local_rules=("F4-METADATA-MACHINE-READABLE",),
    ),
    _item(
        "A1",
        "Can an authorized requester use the identifier and a standardized protocol to reach the resource or its access procedure?",
        (
            "published identifier",
            "documented access protocol and route",
            "observed retrieval or unambiguous request procedure",
        ),
        "Publish and test a stable identifier-to-resource or identifier-to-access-procedure route.",
        local_rules=("A1-RETRIEVAL-PROTOCOL",),
    ),
    _item(
        "A1.1",
        "Is the access protocol open, free to specify, and universally implementable?",
        (
            "protocol identity",
            "freely accessible specification",
            "evidence of universal implementability",
        ),
        "Use a documented open communication protocol and record its accessible specification.",
    ),
    _item(
        "A1.2",
        "Does the access route support authentication and authorization when restrictions require them?",
        (
            "access policy and whether restrictions apply",
            "documented human or machine requester procedure",
            "tested authorization flow when required",
        ),
        "Document whether access is restricted; if so, test the authentication and authorization procedure with the controller.",
    ),
    _item(
        "A2",
        "Will metadata remain accessible when the data are no longer available?",
        (
            "metadata preservation policy",
            "persistent metadata access route",
            "tombstone or withdrawn-record behavior where available",
        ),
        "Use a repository with a stated metadata retention policy and a persistent record for withdrawn data.",
    ),
    _item(
        "I1",
        "Do data or metadata use a shared formal language that machines can interpret as knowledge?",
        (
            "knowledge representation language and specification",
            "parseable entity and relation model",
            "validation of the representation",
        ),
        "Publish a semantic description using a suitable formal language and validate its entity and relation model.",
        local_rules=("I1-DATA-FORMATS-OPEN",),
    ),
    _item(
        "I2",
        "Do data or metadata use defined vocabulary terms from vocabularies that themselves follow FAIR principles?",
        (
            "actual term identifiers in context",
            "resolvable term definitions",
            "vocabulary version, licence, and governance",
            "validation of term use",
        ),
        "Select a maintained domain vocabulary with a curator, map fields to defined terms, and validate their use.",
        local_rules=("I2-VOCABULARY-REFERENCED",),
    ),
    _item(
        "I3",
        "Are links to other data or metadata qualified with a meaningful relation and persistent target identifier?",
        (
            "typed relation to another resource",
            "persistent target identifier",
            "target and predicate validation",
        ),
        "Add qualified version, derivation, or related-resource links using a declared semantic model.",
    ),
    _item(
        "R1",
        "Can prospective users judge fitness for a specific reuse from accurate, relevant, rich attributes?",
        (
            "declared reuse context and domain profile",
            "methods, units, populations, conditions and limitations",
            "curator review of accuracy and relevance",
        ),
        "Document the methods, conditions, units, quality and limitations needed to judge reuse in the intended domain.",
    ),
    _item(
        "R1.1",
        "Are clear, accessible usage terms provided for both the data and their metadata?",
        (
            "nonempty retrievable terms",
            "scope of terms for data and metadata",
            "rights-holder confirmation and compatibility review",
        ),
        "Have the rights holder choose usage terms for data and metadata, then verify that both are discoverable and unambiguous.",
        local_rules=("R1.1-LICENCE-DECLARED",),
    ),
    _item(
        "R1.2",
        "Is detailed origin and processing lineage available for the resource?",
        (
            "responsible parties and purpose",
            "source data and collection conditions",
            "methods and subsequent transformations",
            "ownership and credit where relevant",
        ),
        "Capture how, why, by whom, and under what conditions the data were produced and later processed.",
        local_rules=("R1.2-PROVENANCE-DECLARED",),
    ),
    _item(
        "R1.3",
        "Do data and metadata meet standards relevant to their research community?",
        (
            "declared research domain and selected standard/version",
            "validator results for applicable constraints",
            "reviewed exceptions and domain suitability",
        ),
        "Select the relevant community standard with domain experts and validate actual conformance.",
        local_rules=("R1.3-COMMUNITY-STANDARD", "R1.3-MISSING-VALUES-DECLARED"),
    ),
)

_BY_ID = {item["id"]: item for item in PRINCIPLES}

# One concrete way to supply and recheck each piece of required evidence.
# These are ordered to match each item's required_evidence tuple above.
_EVIDENCE_ACTIONS: dict[str, tuple[str, ...]] = {
    "F1": (
        "Record separate identifiers for the data object and its metadata, with the field or predicate that says which object each denotes; review both links.",
        "Record the issuing service and link its uniqueness and persistence policy; check that it applies to the assigned identifiers.",
        "Resolve each identifier and record the endpoint, time, response, and identity of the returned object.",
    ),
    "F2": (
        "Fill title, creator, publisher, publication date, and identifier with curator-confirmed values where applicable; reject empty placeholders.",
        "Add content and domain descriptors a new user would search for, such as subject, population, method, and time period; review their accuracy.",
        "Declare a discovery metadata schema and run its validator, recording missing required fields.",
    ),
    "F3": (
        "Choose a known metadata-to-data relation or schema field and use it explicitly, rather than relying on a filename in prose.",
        "Put the described data object's assigned identifier in that relation.",
        "Check that the relation points to this dataset or file, not a cited paper or another resource.",
    ),
    "F4": (
        "Record the repository or registry that indexes this metadata record.",
        "Query the registry by the object's identifier and by representative descriptive fields.",
        "Save the returned record identifier, query, endpoint, and observation time; compare it with the assessed object.",
    ),
    "A1": (
        "Record the published identifier used to request this resource.",
        "Document the protocol and landing, download, API, or access-request route reached from that identifier.",
        "Test the route as an authorized requester and record retrieval or the unambiguous next access step.",
    ),
    "A1.1": (
        "Identify the actual communication protocol used by the published access route.",
        "Link a freely available specification for that protocol.",
        "Check that the protocol can be implemented without a vendor-only client or restricted licence.",
    ),
    "A1.2": (
        "Record whether the data are public or restricted and who controls access; do not assume restricted data fail FAIR.",
        "If restricted, document how human or machine requesters authenticate and how authorization decisions are made.",
        "Test an authorized and an unauthorized request without recording credentials in assessment artifacts; if public, document why no authentication is needed.",
    ),
    "A2": (
        "Obtain the repository's metadata retention and withdrawal policy, including what happens when data are removed.",
        "Record a persistent route to the metadata independently of the data download.",
        "Inspect a withdrawn or tombstoned record where available; otherwise retain policy evidence and mark future behavior unverified.",
    ),
    "I1": (
        "Name the formal knowledge representation language and link its accessible specification.",
        "Publish a parseable model that distinguishes entities, attributes, and relationships; a CSV or generic JSON syntax alone is insufficient.",
        "Run the corresponding parser or schema validator and retain its report.",
    ),
    "I2": (
        "Map actual metadata or data fields to specific vocabulary term identifiers; a bare namespace or empty context is insufficient.",
        "Resolve representative term identifiers and check that definitions distinguish their intended meanings.",
        "Record the vocabulary version, licence, maintainer, and stable access route.",
        "Validate that each mapped term is used in the right field and context with a domain reviewer or suitable validator.",
    ),
    "I3": (
        "Add explicit named relations for versions, derivations, or related resources using a declared model.",
        "Use persistent identifiers for the target resources rather than only filenames or unqualified URLs.",
        "Check the relation's meaning and target identity; record resolution when available.",
    ),
    "R1": (
        "Name the intended reuse domain and select its metadata profile with a domain steward.",
        "Record the methods, units, population, conditions, processing, quality, and limits needed to judge suitability.",
        "Have a domain reviewer check that the descriptions are accurate and answer realistic reuse questions.",
    ),
    "R1.1": (
        "Ask the rights holder to choose nonempty usage terms or a licence and make the authoritative text accessible.",
        "State separately which terms govern the data and which govern metadata; check component-level exceptions.",
        "Confirm the rights holder approved the terms and review conflicts among combined resources.",
    ),
    "R1.2": (
        "Record responsible parties and the reason the resource was created.",
        "Describe starting data, collection conditions, and source-resource identifiers.",
        "Document methods, software or processing steps, versions, and later transformations.",
        "Record ownership, credit, and funding or resources where relevant, with curator review.",
    ),
    "R1.3": (
        "Identify the research community and a relevant standard with its version; have a domain expert confirm suitability.",
        "Run that standard's validator on data and metadata and retain the report; a recognizable filename does not establish conformance.",
        "Review exceptions against the standard and document justified departures and remaining gaps.",
    ),
}

# Where an assessor must obtain each requirement's evidence. A local snapshot
# never silently stands in for an external service, policy, or expert review.
_EVIDENCE_ORIGINS: dict[str, tuple[str, ...]] = {
    "F1": ("local", "external_document", "live_publication"),
    "F2": ("local", "local", "validator_report"),
    "F3": ("local", "local", "local"),
    "F4": ("external_document", "live_publication", "live_publication"),
    "A1": ("local", "live_publication", "live_publication"),
    "A1.1": ("live_publication", "external_document", "external_document"),
    "A1.2": ("reviewed_assertion", "external_document", "live_publication"),
    "A2": ("external_document", "live_publication", "live_publication"),
    "I1": ("external_document", "local", "validator_report"),
    "I2": ("local", "live_publication", "external_document", "validator_report"),
    "I3": ("local", "local", "validator_report"),
    "R1": ("reviewed_assertion", "local", "reviewed_assertion"),
    "R1.1": ("live_publication", "local", "reviewed_assertion"),
    "R1.2": ("local", "local", "local", "reviewed_assertion"),
    "R1.3": ("reviewed_assertion", "validator_report", "reviewed_assertion"),
}

# Requirement indexes that cannot be observed against an unpublished dataset.
# Public vocabulary services and standard validators can still be checked now.
_POST_PUBLICATION_REQUIREMENTS: dict[str, frozenset[int]] = {
    "F1": frozenset({2}),
    "F4": frozenset({1, 2}),
    "A1": frozenset({1, 2}),
    "A1.1": frozenset({0}),
    "A1.2": frozenset({2}),
    "A2": frozenset({1, 2}),
}

_LOCAL_GAP_ACTIONS = {
    "F1-PID-METADATA": "Record an assigned identifier for this dataset in recognized metadata; distinguish it from citations to other objects.",
    "F2-METADATA-PRESENT": "Create a recognizable metadata record and re-ingest the package.",
    "F3-METADATA-LINKS-DATA": "Identify the data resources in metadata, then link their assigned identifiers with a known relation.",
    "F4-METADATA-MACHINE-READABLE": "Add structured metadata that a registry can index, then test actual indexing separately.",
    "I1-DATA-FORMATS-OPEN": "Offer a documented exchange format while separately adding interpretable semantics.",
    "I2-VOCABULARY-REFERENCED": "Record actual vocabulary term identifiers; a bare namespace or empty context is insufficient.",
    "R1.1-LICENCE-DECLARED": "Have the rights holder record nonempty usage terms; an empty field grants no rights.",
    "R1.2-PROVENANCE-DECLARED": "Record verified producer or source information, then build detailed lineage.",
    "R1.3-COMMUNITY-STANDARD": "Choose a domain-relevant standard and validate conformance to it.",
    "R1.3-MISSING-VALUES-DECLARED": "Review ambiguous tokens with the data owner and declare their intended meaning before reassessment.",
}


def _evidence_plan(item: dict[str, Any]) -> list[dict[str, str]]:
    requirements = item["required_evidence"]
    actions = _EVIDENCE_ACTIONS[item["id"]]
    origins = _EVIDENCE_ORIGINS[item["id"]]
    if len(requirements) != len(actions) or len(requirements) != len(origins):
        raise ValueError(f"{item['id']}: evidence requirements, origins and actions disagree")
    return [
        {
            "requirement": requirement,
            "evidence_origin": origin,
            "status": "not_verified",
            "action": action,
        }
        for requirement, origin, action in zip(requirements, origins, actions, strict=True)
    ]


def list_principles() -> dict[str, Any]:
    """List all Foundation items, including those without a local rule."""
    return {
        "framework": "GO FAIR Foundation interpretations",
        "source": "https://www.gofair.foundation/interpretations",
        "assessment_scope": "local observations plus explicit evidence gaps",
        "principles": [
            {key: deepcopy(item[key]) for key in ("id", "question", "source", "local_rules")}
            for item in PRINCIPLES
        ],
    }


def get_principle(identifier: str) -> dict[str, Any]:
    """Return the Foundation interpretation contract for one item."""
    if identifier not in _BY_ID:
        raise LookupRejected(
            f"unknown FAIR principle '{echo(identifier)}'; known: {', '.join(_BY_ID)}"
        )
    item = deepcopy(_BY_ID[identifier])
    item["evidence_plan"] = _evidence_plan(item)
    item["acceptance_criteria"] = (
        f"For {identifier}, verify each requirement: "
        + "; ".join(item["required_evidence"])
        + ". Pass only when every requirement has verified evidence bound to this dataset; "
        "fail only for a verified contradiction; otherwise unknown. Narrow local rules "
        "never establish a principle verdict."
    )
    return item


def assess_principles(
    narrow_assessment: dict[str, Any],
    identifier: str | None = None,
    *,
    publication_probe: dict[str, Any] | None = None,
    publication_binding: bool = False,
    unpublished: bool = False,
) -> dict[str, Any]:
    """Analyze local signals and give an action for each unverified requirement.

    A narrow check cannot satisfy a broader Foundation interpretation. A
    public DOI resolver observation establishes the resolver's protocol only;
    it cannot establish that this is the assessed resource's access route.
    """
    if unpublished and publication_probe is not None:
        raise ValueError("an unpublished assessment cannot include a publication probe")
    entries = (
        (get_principle(identifier),)
        if identifier
        else tuple(get_principle(item["id"]) for item in PRINCIPLES)
    )
    by_rule = {result["rule_id"]: result for result in narrow_assessment["results"]}
    results: list[dict[str, Any]] = []
    for item in entries:
        local = [
            deepcopy(by_rule[rule_id]) for rule_id in item["local_rules"] if rule_id in by_rule
        ]
        failing = [result for result in local if result["result"] == "fail"]
        local_gaps = [
            {
                "rule_id": result["rule_id"],
                "scope": "narrow local check",
                "rationale": result.get("rationale", ""),
                "evidence": deepcopy(result["evidence"]),
                "action": _LOCAL_GAP_ACTIONS.get(result["rule_id"], item["next_action"]),
            }
            for result in failing
        ]
        plan = _evidence_plan(item)
        if unpublished:
            for index in _POST_PUBLICATION_REQUIREMENTS.get(item["id"], frozenset()):
                plan[index]["status"] = "pending_publication"
                plan[index]["action"] = (
                    "Prepare this for release, then verify it after publication: "
                    + plan[index]["action"]
                )
        verified_evidence: list[dict[str, Any]] = []
        if publication_probe and publication_binding and publication_probe["result"] == "reached":
            observation = {
                "check": "publication.doi-resolver-http-route",
                "result": "verified",
                "scope": "DOI resolver exchange; dataset access route identity not verified",
                "source": publication_probe["source"],
                "observed_at": publication_probe["observed_at"],
                "method": publication_probe["method"],
                "status_code": publication_probe["status_code"],
                "final_url": publication_probe["final_url"],
            }
            if item["id"] == "A1.1":
                plan[0]["status"] = "observed_partial"
                plan[0]["evidence"] = [observation]
                verified_evidence.append(observation)
        result = "pass" if all(need["status"] == "verified" for need in plan) else "unknown"
        actions = [gap["action"] for gap in local_gaps]
        actions.extend(
            need["action"]
            for need in plan
            if need["status"] != "verified" and need["action"] not in actions
        )
        results.append(
            {
                "principle": item["id"],
                "source": item["source"],
                "question": item["question"],
                "result": result,
                "rationale": (
                    "All required principle-level evidence has been verified."
                    if result == "pass"
                    else "Local checks report narrower observations. Some required evidence "
                    "for the full Foundation interpretation has not been verified."
                ),
                "local_findings": local,
                "observed_local_gaps": local_gaps,
                "required_evidence": list(item["required_evidence"]),
                "acceptance_criteria": item["acceptance_criteria"],
                "evidence_plan": plan,
                "evidence": verified_evidence
                + [
                    {
                        "check": "foundation.evidence-coverage",
                        "result": result,
                        "detail": (
                            "All required evidence verified."
                            if result == "pass"
                            else "Some required principle-level evidence remains unverified."
                        ),
                    }
                ],
                "recommendations": actions,
            }
        )
    return {
        "framework": "GO FAIR Foundation interpretations",
        "source": "https://www.gofair.foundation/interpretations",
        "dataset_id": narrow_assessment["dataset_id"],
        "assessment_scope": "local observations plus explicit evidence gaps",
        "results": results,
        "summary": {
            "total": len(results),
            "pass": sum(item["result"] == "pass" for item in results),
            "unknown": sum(item["result"] == "unknown" for item in results),
        },
        "publication_probe": deepcopy(publication_probe),
        "publication_binding": publication_binding,
        "publication_state": "user_declared_unpublished" if unpublished else "unspecified",
    }
