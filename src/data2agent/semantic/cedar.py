"""Offline HCM profile and CEDAR interoperability; no provider calls.

A report is not an approval. Profile policies constrain candidate values only;
profile structure, evidence and review remain the caller's independent duties.
"""

from __future__ import annotations

import copy
import hashlib
import json
from importlib.resources import files
from pathlib import Path

from .compile import _terms_from_rdf
from .constraints import (
    MappingPolicy,
    OntologyRuntime,
    OntologySnapshot,
    SemanticError,
    sha256_of,
)

CONVERSION_SCHEMA = "d2a-cedar-conversion-1"


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_profile(raw: bytes) -> dict:
    try:
        import yaml
    except ImportError as error:
        raise SemanticError("profile YAML requires PyYAML") from error
    try:

        class UniqueLoader(yaml.SafeLoader):
            pass

        def unique_mapping(loader, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if key in result:
                    raise SemanticError(f"duplicate YAML key: {key}")
                result[key] = loader.construct_object(value_node, deep=deep)
            return result

        UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
        data = yaml.load(raw, Loader=UniqueLoader)
    except (yaml.YAMLError, UnicodeDecodeError) as error:
        raise SemanticError("invalid profile YAML") from error
    if (
        not isinstance(data, dict)
        or data.get("profile_id") != "hcm-recording-minimal"
        or str(data.get("version")) != "1"
    ):
        raise SemanticError("unsupported profile")
    return data


def compile_profile(profile_bytes: bytes, source_root: Path) -> dict:
    """Verify pinned source bytes and compile an offline candidate policy and loss report.

    The returned policy does not implement the complete HCM profile: records need
    the profile's separate structure/evidence/review validator. No value is approved.
    """
    profile = _load_profile(profile_bytes)
    root = Path(source_root).resolve(strict=True)
    sources = profile.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise SemanticError("profile has no pinned sources")
    verified = {}
    for key, source in sorted(sources.items()):
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            raise SemanticError(f"invalid source {key}")
        path = (root / source["path"]).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise SemanticError(f"source {key} escapes the source root")
        digest = _digest(path.read_bytes())
        if digest != source.get("sha256") or not source.get("version"):
            raise SemanticError(f"source {key} digest or version mismatch")
        verified[key] = (source, path)
    fields = profile.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise SemanticError("profile has no fields")
    known_top = {
        "profile_id",
        "version",
        "status",
        "source_commit",
        "missing_semantics",
        "sources",
        "fields",
        "mapping_record",
    }
    if set(profile) - known_top:
        raise SemanticError("unknown profile constraints")
    terms = {}
    onts = {}
    source_records = []
    for name in ("ontology", "modality", "technology"):
        if name not in verified:
            raise SemanticError(f"missing pinned {name} source")
        source, path = verified[name]
        onts[name] = {"version": source["version"]}
        loaded = _terms_from_rdf(path, "turtle", name, "all", "d2a-ontology-snapshot-2")
        for term in loaded:
            if term["iri"] in terms:
                raise SemanticError("duplicate term in profile sources")
            terms[term["iri"]] = term
        source_records.append(
            {"ontology": name, **source, "origin": source["path"], "status": "profile-pinned"}
        )
    for term in terms.values():
        external = [p for p in term.get("parents", []) if p not in terms]
        term["parents"] = [p for p in term.get("parents", []) if p in terms]
        if external:
            term["external_parents"] = external
    snapshot_data = {
        "schema_version": "d2a-ontology-snapshot-2",
        "snapshot_id": f"{profile['profile_id']}@{profile['version']}:{_digest(profile_bytes)}",
        "prefixes": {},
        "ontologies": onts,
        "sources": source_records,
        "schemes": sorted(
            {scheme for term in terms.values() for scheme in term.get("schemes", [])}
        ),
        "terms": [terms[x] for x in sorted(terms)],
    }
    snapshot = OntologySnapshot(snapshot_data)
    policy_fields = {}
    field_reports = {}
    for field_id, field in sorted(fields.items()):
        if not isinstance(field, dict):
            raise SemanticError(f"{field_id}: field must be an object")
        rule = field.get("policy") or {}
        if not isinstance(rule, dict):
            raise SemanticError(f"{field_id}: policy must be an object")
        entry = {
            "target": {"entity": field.get("entity"), "property": field.get("property")},
            "value_type": field.get("value_type"),
            "cardinality": {"min": field.get("min_count"), "max": field.get("max_count")},
            "source_constraints": rule,
            "source_field": field,
            "status": "blocked",
            "codes": [],
        }
        if field.get("value_type") == "mapping" and rule.get("scheme"):
            if set(rule) - {"source", "scheme", "allowed", "kind", "hierarchy"}:
                entry["codes"] = ["unknown_profile_constraint"]
                field_reports[field_id] = entry
                continue
            if rule.get("kind") != "skos:Concept" or rule.get("hierarchy") != "skos:broader":
                raise SemanticError(f"{field_id}: unsupported concept semantics")
            scheme, allowed = rule["scheme"], rule.get("allowed")
            if not isinstance(allowed, list) or not allowed:
                raise SemanticError(f"{field_id}: missing explicit allowed concepts")
            for iri in allowed:
                term = snapshot.terms.get(iri)
                if (
                    not term
                    or term["kind"] != "concept"
                    or term["ontology"] != rule.get("source")
                    or scheme not in term.get("schemes", [])
                    or term.get("deprecated")
                ):
                    raise SemanticError(f"{field_id}: invalid scheme member {iri}")
            policy_fields[field_id] = {
                "datatype": "iri",
                "term_kinds": ["concept"],
                "allowed": {"schemes": [scheme], "terms": sorted(allowed)},
                "cardinality": entry["cardinality"],
            }
            entry["admissible_iris"] = sorted(allowed)
            entry["codes"] = ["cedar_skos_source_unverified", "review_and_evidence_external"]
        elif field.get("value_type") == "quantity":
            entry["codes"] = ["structured_quantity_unsupported"]
        elif field.get("value_type") == "mapping":
            entry["codes"] = ["normalization_source_unpinned"]
        else:
            entry["codes"] = ["profile_value_restrictions_not_proven_equivalent"]
        field_reports[field_id] = entry
    policy_data = {
        "schema_version": "d2a-mapping-policy-2",
        "policy_id": profile["profile_id"] + "-candidate-policy",
        "version": str(profile["version"]),
        "closed_world": True,
        "snapshot": {"snapshot_id": snapshot.snapshot_id, "sha256": snapshot.sha256},
        "fields": policy_fields,
    }
    policy = MappingPolicy(policy_data, snapshot)
    runtime = OntologyRuntime(snapshot, policy)
    for field_id in policy_fields:
        if list(runtime.admissible_terms(field_id)) != field_reports[field_id]["admissible_iris"]:
            raise SemanticError(f"{field_id}: policy does not preserve the explicit set")
    return {
        "schema_version": CONVERSION_SCHEMA,
        "status": "blocked",
        "source": {
            "id": profile["profile_id"],
            "version": str(profile["version"]),
            "raw_sha256": _digest(profile_bytes),
            "canonical_sha256": sha256_of(profile),
            "sources": {k: v[0] for k, v in verified.items()},
        },
        "snapshot": snapshot_data,
        "snapshot_sha256": snapshot.sha256,
        "policy": policy_data,
        "policy_sha256": policy.sha256,
        "fields": field_reports,
        "artifact": None,
        "limitations": [
            "candidate policy only; structure, evidence and review require "
            "independent profile validation",
            "no CEDAR artifact is publishable from blocked fields",
        ],
    }


def export_profile(profile_bytes: bytes, source_root: Path) -> dict:
    """Return the deterministic loss report; blocked fields are never exported."""
    return compile_profile(profile_bytes, source_root)


def export_profile_draft(profile_bytes: bytes, source_root: Path) -> dict:
    """Produce a partial, unpersisted CEDAR 1.6 draft and an enforcement manifest.

    The existing conversion-1 API is unchanged. This draft cannot be published or
    interpreted as validating the profile; omissions and external rules are explicit.
    """
    compiled = compile_profile(profile_bytes, source_root)
    profile = _load_profile(profile_bytes)
    baseline = json.loads(files(__package__).joinpath("cedar_baseline.json").read_text("utf-8"))
    template = baseline["template"]

    def clear_identity(item):
        for name in (
            "@id",
            "pav:createdOn",
            "pav:createdBy",
            "pav:lastUpdatedOn",
            "oslc:modifiedBy",
        ):
            item[name] = None

    clear_identity(template)
    template["schema:name"] = profile["profile_id"]
    template["schema:description"] = "Partial local draft of the pinned HCM profile"
    template["title"] = profile["profile_id"] + " template schema"
    template["description"] = "Partial local draft; independent profile validation is required"
    template["pav:version"] = "0.0.1"
    template["bibo:status"] = "bibo:draft"
    original = compiled["fields"]
    snapshot = OntologySnapshot(compiled["snapshot"])
    entries = {}
    allowed_keys = {
        "definition",
        "entity",
        "property",
        "value_type",
        "min_count",
        "max_count",
        "policy",
        "evidence",
        "review",
        "datatype",
    }
    xsd = "http://www.w3.org/2001/XMLSchema#"
    for key, field in sorted(profile["fields"].items()):
        entry = copy.deepcopy(original[key])
        entry["status"] = "blocked"
        entry["artifact_field"] = None
        codes = []
        entity = snapshot.terms.get(field.get("entity"), {})
        prop = snapshot.terms.get(field.get("property"), {})
        kind = field.get("value_type")
        policy = field.get("policy")
        if set(field) - allowed_keys:
            codes.append("unknown_field_constraint")
        if (
            not isinstance(field.get("min_count"), int)
            or isinstance(field.get("min_count"), bool)
            or field.get("min_count") not in (0, 1)
            or field.get("max_count") != 1
            or isinstance(field.get("max_count"), bool)
        ):
            codes.append("cardinality_not_supported")
        expected_property_type = "object" if kind == "quantity" else "datatype"
        if entity.get("kind") != "class" or prop.get("property_type") != expected_property_type:
            codes.append("target_not_verified_property_type")
        if not isinstance(field.get("definition"), str) or not field["definition"].strip():
            codes.append("missing_definition")
        if kind == "string" and field.get("datatype") == xsd + "string" and policy == {}:
            example = "Text Field"
        elif (
            kind == "date"
            and field.get("datatype") == xsd + "date"
            and policy == {"lexical_subset": "YYYY-MM-DD without timezone, years 0001–9999"}
        ):
            example = "Datetime Field - Date (YYYY-MM-DD)"
        elif (
            kind == "quantity"
            and field.get("datatype") is None
            and policy
            == {
                "class": "http://qudt.org/schema/qudt/QuantityValue",
                "numeric_property": "http://qudt.org/schema/qudt/numericValue",
                "unit_property": "http://qudt.org/schema/qudt/hasUnit",
                "allowed_units": ["http://qudt.org/vocab/unit/HZ"],
                "minimum_exclusive": 0,
            }
        ):
            example = "quantity"
        else:
            example = None
            codes.append("value_constraint_not_supported")
        if codes:
            entry["codes"] = sorted(set(entry["codes"] + codes))
            entries[key] = entry
            continue
        if example == "quantity":
            cedar_field = copy.deepcopy(baseline["element"])
            for child, exemplar, prop_iri in (
                ("numeric_value", "Numeric - Basic", policy["numeric_property"]),
                ("unit", "Link", policy["unit_property"]),
            ):
                subfield = copy.deepcopy(baseline["fields"][exemplar])
                subfield["schema:name"] = child
                clear_identity(subfield)
                subfield["schema:description"] = prop_iri
                subfield["title"] = child + " field schema"
                subfield["description"] = prop_iri
                subfield["_valueConstraints"]["requiredValue"] = True
                cedar_field["properties"][child] = subfield
                cedar_field["properties"]["@context"]["properties"][child] = {"enum": [prop_iri]}
                cedar_field["properties"]["@context"]["required"].append(child)
                cedar_field["required"].append(child)
                cedar_field["_ui"]["order"].append(child)
                cedar_field["_ui"]["propertyLabels"][child] = child
                cedar_field["_ui"]["propertyDescriptions"][child] = ""
        else:
            cedar_field = copy.deepcopy(baseline["fields"][example])
        clear_identity(cedar_field)
        cedar_field["schema:name"] = key
        cedar_field["schema:description"] = field["definition"]
        cedar_field["title"] = key + " field schema"
        cedar_field["description"] = field["definition"]
        if example != "quantity":
            cedar_field["_valueConstraints"]["requiredValue"] = bool(field["min_count"])
        template["properties"][key] = cedar_field
        template["properties"]["@context"]["properties"][key] = {"enum": [field["property"]]}
        template["properties"]["@context"]["required"].append(key)
        template["required"].append(key)
        template["_ui"]["order"].append(key)
        template["_ui"]["propertyLabels"][key] = key
        template["_ui"]["propertyDescriptions"][key] = field["definition"]
        entry["status"] = "partial"
        entry["codes"] = ["independent_profile_validation_required"]
        if example == "quantity":
            entry["codes"] += [
                "quantity_unit_and_positive_value_external",
                "numeric_decimal_ui_subset",
            ]
        entry["artifact_field"] = key
        entries[key] = entry
    has_fields = any(entry["status"] == "partial" for entry in entries.values())
    return {
        "schema_version": "d2a-cedar-draft-export-1",
        "status": "partial" if has_fields else "blocked",
        "publication_status": "blocked",
        "artifact": template if has_fields else None,
        "artifact_sha256": sha256_of(template) if has_fields else None,
        "fields": entries,
        "compile_report": compiled,
        "enforcement": {
            "source_profile_sha256": compiled["source"]["raw_sha256"],
            "required": [
                "Validate the full pinned profile and SHACL independently, including absent, "
                "null and nonblank values, per-entity cardinality, lexical subsets and QUDT. "
                "Quantity units must equal the pinned allowed unit, numeric values must be "
                "positive, and the CEDAR decimal editor does not cover all numeric lexicals.",
                "Preserve raw literal values and independently validate annotation evidence "
                "and review; candidate terms are not scientific validation.",
                "Do not publish this incomplete draft; blocked fields are omitted from CEDAR.",
            ],
            "field_constraints": {
                k: {
                    "target": v["target"],
                    "source_field": v["source_field"],
                    "cedar_status": v["status"],
                    "external_validation_required": True,
                }
                for k, v in entries.items()
            },
            "omitted_fields": sorted(k for k, v in entries.items() if v["status"] == "blocked"),
            "source_mapping_record": profile.get("mapping_record"),
            "source_missing_semantics": profile.get("missing_semantics"),
        },
    }


def import_template(
    raw_bytes: bytes,
    snapshot: OntologySnapshot,
    field_targets: dict,
    *,
    expected_source: dict,
    approval: dict | None = None,
) -> dict:
    """Inspect a local CEDAR JSON template. Unproven representations are blocked.

    This first version does not authorize execution or materialize remote value sets.
    Identity and expected raw digest are always checked before interpretation.
    """

    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SemanticError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        artifact = json.loads(raw_bytes, object_pairs_hook=no_duplicates)
    except (ValueError, UnicodeDecodeError) as error:
        raise SemanticError("invalid CEDAR JSON") from error
    if not isinstance(artifact, dict):
        raise SemanticError("CEDAR artifact must be an object")
    identity = {
        "id": artifact.get("@id"),
        "version": artifact.get("pav:version"),
        "raw_sha256": _digest(raw_bytes),
        "canonical_sha256": sha256_of(artifact),
    }
    if any(identity.get(k) != expected_source.get(k) for k in ("id", "version", "raw_sha256")):
        raise SemanticError("CEDAR source identity, version or digest mismatch")
    if not identity["id"] or not identity["version"]:
        raise SemanticError("CEDAR source identity and version are required")
    if not isinstance(field_targets, dict) or not isinstance(expected_source, dict):
        raise SemanticError("bindings and source pin must be objects")
    properties = artifact.get("properties")
    ui = artifact.get("_ui")
    order = ui.get("order") if isinstance(ui, dict) else None
    if (
        not isinstance(properties, dict)
        or not isinstance(order, list)
        or any(not isinstance(x, str) or not x for x in order)
    ):
        raise SemanticError("invalid CEDAR template structure")
    fields = {}
    candidate_fields = {}
    context_schema = properties.get("@context")
    if not isinstance(context_schema, dict) or not isinstance(
        context_schema.get("properties"), dict
    ):
        raise SemanticError("invalid CEDAR context schema")
    context = context_schema["properties"]
    expected_ontologies = expected_source.get("ontologies", {})
    if not isinstance(expected_ontologies, dict):
        raise SemanticError("ontology pins must be an object")
    field_keys = {
        "@type",
        "@context",
        "type",
        "title",
        "description",
        "_ui",
        "_valueConstraints",
        "properties",
        "schema:name",
        "schema:description",
        "pav:createdOn",
        "pav:createdBy",
        "pav:lastUpdatedOn",
        "oslc:modifiedBy",
        "schema:schemaVersion",
        "additionalProperties",
        "@id",
        "$schema",
        "pav:version",
        "bibo:status",
        "skos:prefLabel",
        "skos:altLabel",
    }
    standard_properties = {
        "@type": {
            "oneOf": [
                {"type": "string", "format": "uri"},
                {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "format": "uri"},
                    "uniqueItems": True,
                },
            ]
        },
        "rdfs:label": {"type": ["string", "null"]},
        "@id": {"type": "string", "format": "uri"},
    }
    for field_id in order:
        field = properties.get(field_id)
        codes = []
        if not isinstance(field_id, str) or not isinstance(field, dict):
            fields[str(field_id)] = {"status": "blocked", "codes": ["invalid_field"]}
            continue
        constraints = field.get("_valueConstraints")
        target = field_targets.get(field_id)
        if (
            not isinstance(target, dict)
            or not isinstance(target.get("property"), str)
            or not isinstance(target.get("entity"), str)
            or target.get("datatype") != "iri"
            or snapshot.terms.get(target.get("entity"), {}).get("kind") != "class"
        ):
            codes.append("missing_or_incompatible_target")
        elif context.get(field_id) != {"enum": [target["property"]]}:
            codes.append("target_property_mismatch")
        elif (
            snapshot.terms.get(target["property"], {}).get("kind") != "property"
            or snapshot.terms[target["property"]].get("property_type") != "object"
        ):
            codes.append("target_not_verified_object_property")
        if (
            set(field) - field_keys
            or field.get("@type") != "https://schema.metadatacenter.org/core/TemplateField"
            or field.get("type") != "object"
            or field.get("additionalProperties") is not False
            or field.get("properties") != standard_properties
            or field.get("_ui") != {"inputType": "textfield"}
        ):
            codes.append("unsupported_field_structure")
        if not isinstance(constraints, dict):
            codes.append("missing_value_constraints")
            constraints = {}
        elif set(constraints) - {
            "requiredValue",
            "multipleChoice",
            "classes",
            "branches",
            "ontologies",
            "valueSets",
        }:
            codes.append("unsupported_constraints")
        if (
            not isinstance(constraints.get("requiredValue"), bool)
            or constraints.get("multipleChoice") is not False
        ):
            codes.append("unsupported_cardinality")
        if constraints.get("requiredValue") is True:
            codes.append("required_presence_not_enforced_by_runtime")
        if any(constraints.get(key) for key in ("branches", "ontologies", "valueSets")):
            codes.append("unsupported_controlled_source")
        classes = constraints.get("classes")
        terms = []
        if not isinstance(classes, list) or not classes:
            codes.append("missing_explicit_classes")
        else:
            for item in classes:
                if not isinstance(item, dict) or set(item) - {
                    "uri",
                    "prefLabel",
                    "type",
                    "label",
                    "source",
                }:
                    codes.append("unsupported_class_record")
                    continue
                iri, source = item.get("uri"), item.get("source")
                term = snapshot.terms.get(iri) if isinstance(iri, str) else None
                if not isinstance(source, str):
                    codes.append("class_or_source_not_pinned")
                    continue
                pin = expected_ontologies.get(source, {})
                if not isinstance(pin, dict):
                    codes.append("class_or_source_not_pinned")
                    continue
                sources = [x for x in snapshot.data["sources"] if x.get("ontology") == source]
                if (
                    item.get("type") != "OntologyClass"
                    or not term
                    or term.get("kind") != "class"
                    or term.get("ontology") != source
                    or term.get("deprecated")
                    or not pin.get("version")
                    or not pin.get("sha256")
                    or len(sources) != 1
                    or sources[0].get("version") != pin["version"]
                    or sources[0].get("sha256") != pin["sha256"]
                ):
                    codes.append("class_or_source_not_pinned")
                else:
                    terms.append(iri)
        if not codes:
            candidate_fields[field_id] = {
                "datatype": "iri",
                "term_kinds": ["class"],
                "allowed": {"terms": sorted(set(terms))},
                "cardinality": {"min": int(constraints["requiredValue"]), "max": 1},
            }
        fields[field_id] = {
            "status": "blocked" if codes else "exact",
            "codes": sorted(set(codes)),
            "target": target,
            "source_constraints": constraints,
            "admissible_iris": sorted(set(terms)) if not codes else [],
        }
    # Unknown schema constraints or missing bindings cannot authorize execution.
    template_keys = {
        "@id",
        "@type",
        "@context",
        "type",
        "title",
        "description",
        "_ui",
        "properties",
        "required",
        "schema:name",
        "schema:description",
        "pav:createdOn",
        "pav:createdBy",
        "pav:lastUpdatedOn",
        "oslc:modifiedBy",
        "schema:schemaVersion",
        "additionalProperties",
        "pav:version",
        "bibo:status",
        "$schema",
    }
    global_codes = []
    if (
        set(artifact) - template_keys
        or artifact.get("additionalProperties") is not False
        or artifact.get("@type") != "https://schema.metadatacenter.org/core/Template"
        or artifact.get("type") != "object"
        or artifact.get("$schema") != "http://json-schema.org/draft-04/schema#"
        or artifact.get("schema:schemaVersion") != "1.6.0"
        or set(ui) - {"order", "propertyLabels", "propertyDescriptions"}
        or not isinstance(artifact.get("required"), list)
        or any(not isinstance(x, str) for x in artifact["required"])
        or not set(order).issubset(artifact["required"])
    ):
        global_codes.append("unsupported_template_structure")
    known_properties = {
        "@context",
        "@id",
        "@type",
        "schema:isBasedOn",
        "schema:name",
        "schema:description",
        "pav:derivedFrom",
        "pav:createdOn",
        "pav:createdBy",
        "pav:lastUpdatedOn",
        "oslc:modifiedBy",
    }
    if set(properties) - known_properties - set(order):
        global_codes.append("unknown_template_property")
    metadata_properties = {
        k: v for k, v in properties.items() if k not in order and k != "@context"
    }
    context_metadata = {
        **context_schema,
        "properties": {k: v for k, v in context.items() if k not in order},
    }
    # Fingerprints pin the complete metadata and context schema shapes of the
    # official fixture, leaving only named field bindings variable.
    if (
        sha256_of(metadata_properties)
        != "96c03c6f5cf8a1fe57f605607979c629a527263a031dc6eebd477fb1354b3e0d"
    ):
        global_codes.append("unsupported_metadata_schema")
    if (
        sha256_of(context_metadata)
        != "3ab3dfdfcf75092de050556ee318e306f21739e3202ec2b62dcd1e1f8e2528a5"
    ):
        global_codes.append("unsupported_context_schema")
    required_metadata = {
        "@context",
        "@id",
        "schema:isBasedOn",
        "schema:name",
        "schema:description",
        "pav:createdOn",
        "pav:createdBy",
        "pav:lastUpdatedOn",
        "oslc:modifiedBy",
    }
    if (
        not isinstance(artifact.get("required"), list)
        or any(not isinstance(x, str) for x in artifact["required"])
        or set(artifact["required"]) != required_metadata | set(order)
    ):
        global_codes.append("unsupported_required_schema")
    if len(order) != len(set(order)) or set(field_targets) != set(order):
        global_codes.append("incomplete_target_bindings")
    if not fields or any(x["status"] != "exact" for x in fields.values()):
        global_codes.append("blocked_fields")
    candidate = None
    if candidate_fields:
        candidate = {
            "schema_version": "d2a-mapping-policy-1",
            "policy_id": identity["id"],
            "version": identity["version"],
            "snapshot": {"snapshot_id": snapshot.snapshot_id, "sha256": snapshot.sha256},
            "closed_world": True,
            "fields": candidate_fields,
        }
        MappingPolicy(candidate, snapshot)
    binding = {
        "source": identity,
        "snapshot_sha256": snapshot.sha256,
        "target_bindings": field_targets,
        "candidate_policy_sha256": sha256_of(candidate),
    }
    binding_sha256 = sha256_of(binding)
    approved = (
        not global_codes
        and isinstance(approval, dict)
        and approval.get("binding_sha256") == binding_sha256
        and isinstance(approval.get("approved_by"), str)
        and bool(approval["approved_by"].strip())
    )
    return {
        "schema_version": CONVERSION_SCHEMA,
        "status": "blocked" if global_codes else "exact",
        "source": identity,
        "snapshot_sha256": snapshot.sha256,
        "target_bindings": field_targets,
        "fields": fields,
        "codes": global_codes,
        "candidate_policy": candidate,
        "candidate_policy_sha256": sha256_of(candidate),
        "binding_sha256": binding_sha256,
        "policy": candidate if approved else None,
        "policy_sha256": sha256_of(candidate) if approved else None,
        "approved": approved,
        "approval": approval,
        "artifact": None,
    }


def export_class_policy(
    policy: dict,
    source_template_bytes: bytes,
    snapshot: OntologySnapshot,
    field_targets: dict,
    *,
    expected_source: dict,
    approval: dict,
) -> dict:
    """Recover a verified original template for an unchanged approved class-list policy.

    This reverse operation deliberately requires the original bytes: changing an
    identified CEDAR artifact while retaining its ID would falsely claim identity.
    No template IDs or ontology terms are minted.
    """
    report = import_template(
        source_template_bytes,
        snapshot,
        field_targets,
        expected_source=expected_source,
        approval=approval,
    )
    if not report["approved"] or report["policy_sha256"] != sha256_of(policy):
        report["status"] = "blocked"
        report["codes"] = sorted(set(report["codes"] + ["policy_or_approval_mismatch"]))
        report["artifact"] = None
        return report
    report["artifact"] = json.loads(source_template_bytes)
    report["artifact_raw_sha256"] = _digest(source_template_bytes)
    return report
