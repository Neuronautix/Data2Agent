"""The optional semantic layer: source evidence -> versioned semantic mapping record.

Deterministic ingestion, the immutable manifest and the evidence ledger come
first and are never touched here. This module reads them, reads the source only
to verify bytes it quotes, and writes one sidecar, ``semantic/semantic-mapping.json``
(``d2a-semantic-mapping-1``), bound to the dataset id and the manifest and ledger
hashes, exactly as ``relationships.json`` is.

Every value in the record carries one of four statuses, never blurred:

``verified_source_fact``
    the raw value as the source holds it, with the ledger claims that attest the
    column or file and the file checksum it was read under;
``deterministic_transformation``
    the raw value equals, case-insensitively, exactly one authorised term's label
    or synonym in the field's scope; the IRI follows by lookup, not by judgment;
``inferred_mapping``
    a term chosen by an external selector (a model, an expert) and imported with
    ``inferred``; it is recorded only when it passes the same independent
    validation, with the selector's identity, and stays an inference;
``unresolved``
    anything else: no exact match, several exact matches, values not exhaustively
    known, an inadmissible inference, or an unavailable vocabulary. Retrieved
    candidates are listed as considered, never committed. Fuzzy-matching a value
    to its nearest term would be inference presented as lookup, so it is not done.

The record carries no timestamp, so two runs over the same inputs write the same
bytes. It is optional: nothing in ``ingest``, ``verify``, ``assess`` or ``serve``
reads or writes it unless the caller asks.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ..ingest.checksum import hash_bytes, hash_file
from .constraints import (
    ENGINE_VERSION,
    FORMAL_ONLY_NOTE,
    OntologyRuntime,
    SemanticError,
    canonical_json,
    sha256_of,
)

BINDINGS_SCHEMA = "d2a-semantic-bindings-1"
INFERRED_SCHEMA = "d2a-semantic-inferred-1"
RECORD_CONTRACT = "d2a-semantic-mapping-1"
SEMANTIC_DIRNAME = "semantic"
RECORD_FILENAME = "semantic-mapping.json"
STATUSES = (
    "verified_source_fact",
    "deterministic_transformation",
    "inferred_mapping",
    "unresolved",
)
CANDIDATES_CONSIDERED = 3
_MAX_JSON_BYTES = 8 * 1024 * 1024
_RECORD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.json$")


def load_bindings(path: Path) -> tuple[list[dict], str]:
    """A ``d2a-semantic-bindings-1`` file: which source field fills which policy field."""
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema_version") != BINDINGS_SCHEMA:
        raise SemanticError(f"bindings must declare schema_version {BINDINGS_SCHEMA}")
    bindings = data.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        raise SemanticError("bindings needs a non-empty bindings list")
    for binding in bindings:
        source = binding.get("source") if isinstance(binding, dict) else None
        if not isinstance(source, dict) or set(source) not in (
            {"table", "column"},
            {"file", "key"},
        ):
            raise SemanticError("each binding needs a source {table, column} or {file, key}")
        if not isinstance(binding.get("field"), str):
            raise SemanticError("each binding needs a policy field id")
    return bindings, hash_bytes(raw)


def load_inferred(path: Path) -> tuple[list[dict], str]:
    """A ``d2a-semantic-inferred-1`` file: selector decisions to validate, kept as inferences."""
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema_version") != INFERRED_SCHEMA:
        raise SemanticError(f"inferred mappings must declare schema_version {INFERRED_SCHEMA}")
    selector = data.get("selector")
    if not isinstance(selector, dict) or not selector.get("name"):
        raise SemanticError("inferred mappings must name their selector (model, expert or method)")
    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        raise SemanticError("inferred mappings need a decisions list")
    return [{**d, "selector": selector} for d in decisions], hash_bytes(raw)


def _source_key(source: dict) -> str:
    return (
        f"{source['table']}:{source['column']}"
        if "table" in source
        else f"{source['file']}#{source['key']}"
    )


class _Inputs:
    """The ingest outputs, read once, with the hashes the record binds to."""

    def __init__(self, output_dir: Path, source_dir: Path | None):
        self.output_dir = Path(output_dir)
        self.manifest_sha256 = hash_file(self.output_dir / "manifest.json")
        self.evidence_sha256 = hash_file(self.output_dir / "evidence.json")
        self.manifest = json.loads((self.output_dir / "manifest.json").read_text(encoding="utf-8"))
        evidence = json.loads((self.output_dir / "evidence.json").read_text(encoding="utf-8"))
        if evidence.get("dataset_id") != self.manifest.get("dataset_id"):
            raise SemanticError("manifest.json and evidence.json describe different datasets")
        self.claims = evidence.get("claims", [])
        self.files = {entry["path"]: entry for entry in self.manifest.get("files", [])}
        if source_dir is None:
            provenance = json.loads(
                (self.output_dir / "provenance.json").read_text(encoding="utf-8")
            )
            source_dir = provenance.get("source_path")
        self.source_dir = Path(source_dir) if source_dir else None

    def claims_about(self, subject: str, field: str | None, checks: tuple[str, ...]) -> list[str]:
        found = []
        for claim in self.claims:
            if claim.get("subject") != subject:
                continue
            for item in claim.get("evidence", []):
                if item.get("check") in checks and (field is None or item.get("field") == field):
                    found.append(claim["claim_id"])
                    break
        return sorted(set(found))

    def source_fact(self, source: dict) -> dict:
        """The raw values a binding's source holds, with the claims and checksum attesting them."""
        if "table" in source:
            table = self.manifest.get("tables", {}).get(source["table"])
            column = next(
                (c for c in (table or {}).get("columns", []) if c.get("name") == source["column"]),
                None,
            )
            if column is None:
                return {
                    "status": "unresolved",
                    "reason": f"{_source_key(source)} is not a profiled column",
                }
            claims = self.claims_about(
                source["table"],
                source["column"],
                ("table.column-dtype", "table.missing-value-count"),
            )
            claims += self.claims_about(source["table"], None, ("file.checksum",))
            base = {
                "source_sha256": self.files.get(source["table"], {}).get("sha256"),
                "evidence_claim_ids": sorted(set(claims)),
                "read_from": "manifest.json column profile",
            }
            if not column.get("distinct_exact") or "distinct_values" not in column:
                return {
                    **base,
                    "status": "unresolved",
                    "reason": "the manifest does not hold the column's exhaustive distinct values",
                }
            return {
                **base,
                "status": "verified_source_fact",
                "raw_values": list(column["distinct_values"]),
            }
        entry = self.files.get(source["file"])
        if entry is None:
            return {"status": "unresolved", "reason": f"{source['file']} is not in the manifest"}
        claims = self.claims_about(source["file"], None, ("file.checksum", "json.shape"))
        base = {
            "source_sha256": entry["sha256"],
            "evidence_claim_ids": claims,
            "read_from": "source file, re-hashed",
        }
        if self.source_dir is None:
            return {
                **base,
                "status": "unresolved",
                "reason": "no source directory to read the key from",
            }
        path = self.source_dir / source["file"]
        try:
            with path.open("rb") as handle:  # never more than the cap plus one byte in memory
                raw = handle.read(_MAX_JSON_BYTES + 1)
        except OSError as error:
            return {
                **base,
                "status": "unresolved",
                "reason": f"source file unreadable: {error.strerror}",
            }
        if len(raw) > _MAX_JSON_BYTES:
            return {
                **base,
                "status": "unresolved",
                "reason": f"source file exceeds {_MAX_JSON_BYTES} bytes; nothing read",
            }
        if hash_bytes(raw) != entry["sha256"]:
            return {
                **base,
                "status": "unresolved",
                "reason": "source file no longer matches the manifest checksum; nothing read",
            }
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {**base, "status": "unresolved", "reason": "source file is not JSON"}
        if not isinstance(document, dict) or source["key"] not in document:
            return {**base, "status": "unresolved", "reason": f"key {source['key']!r} is absent"}
        value = document[source["key"]]
        values = value if isinstance(value, list) else [value]
        if not values or not all(
            isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in values
        ):
            return {**base, "status": "unresolved", "reason": "the key does not hold scalar values"}
        return {**base, "status": "verified_source_fact", "raw_values": values}


def _exact_matches(runtime: OntologyRuntime, field_id: str, raw: Any) -> list[str]:
    text = str(raw).strip().lower()
    hits = []
    for iri in runtime.admissible_terms(field_id):
        term = runtime.snapshot.terms[iri]
        names = [str(term.get("label") or "").lower()] + [
            str(s).lower() for s in term.get("synonyms") or []
        ]
        if text and text in names:
            hits.append(iri)
    return hits


def build_semantic_record(
    output_dir: Path,
    runtime: OntologyRuntime,
    bindings: list[dict],
    *,
    bindings_sha256: str,
    source_dir: Path | None = None,
    inferred: list[dict] | None = None,
    inferred_sha256: str | None = None,
) -> dict:
    """The ``d2a-semantic-mapping-1`` record of one ingested dataset; never writes its inputs."""
    inputs = _Inputs(output_dir, source_dir)
    inferred_by_key: dict[tuple[str, str, str], dict] = {}
    for decision in inferred or []:
        source = decision.get("source")
        if not isinstance(source, dict) or set(source) not in (
            {"table", "column"},
            {"file", "key"},
        ):
            raise SemanticError(
                "each inferred decision needs a source {table, column} or {file, key}"
            )
        key = (_source_key(source), str(decision.get("field")), str(decision.get("raw_value")))
        if key in inferred_by_key:
            raise SemanticError(f"two inferred decisions for {key}")
        inferred_by_key[key] = decision
    used: set[tuple[str, str, str]] = set()
    entries = []
    for binding in bindings:
        source, field_id = binding["source"], binding["field"]
        if field_id not in runtime.policy.fields:
            raise SemanticError(
                f"binding names field {field_id!r}, which policy {runtime.policy.policy_id} lacks"
            )
        fact = inputs.source_fact(source)
        entry = {
            "source": source,
            "source_key": _source_key(source),
            "field": field_id,
            "source_fact": fact,
            "values": [],
        }
        for raw in fact.get("raw_values", []):
            entry["values"].append(
                _map_value(runtime, field_id, source, raw, inferred_by_key, used)
            )
        entries.append(entry)
    stray = sorted(set(inferred_by_key) - used)
    rejected = [
        {
            "source_key": k[0],
            "field": k[1],
            "raw_value": k[2],
            "reason": "raw value is not a verified source fact of a bound field (fidelity)",
        }
        for k in stray
    ]
    counts = {status: 0 for status in STATUSES}
    for entry in entries:
        counts["verified_source_fact"] += len(entry["values"])
        for value in entry["values"]:
            counts[value["status"]] += 1
        if entry["source_fact"]["status"] == "unresolved":
            counts["unresolved"] += 1
    return {
        "contract": RECORD_CONTRACT,
        "dataset_id": inputs.manifest.get("dataset_id"),
        "manifest_sha256": inputs.manifest_sha256,
        "evidence_sha256": inputs.evidence_sha256,
        "engine": runtime.identity(),
        "bindings_sha256": bindings_sha256,
        "inferred_sha256": inferred_sha256,
        "statuses": list(STATUSES),
        "note": FORMAL_ONLY_NOTE,
        "entries": entries,
        "rejected_inferences": rejected,
        "summary": counts,
    }


def _map_value(
    runtime: OntologyRuntime,
    field_id: str,
    source: dict,
    raw: Any,
    inferred_by_key: dict,
    used: set,
) -> dict:
    """One verified raw value through one flow, for IRI and literal fields alike.

    verified source fact -> deterministic exact match -> matching external inference ->
    independent validation -> deterministic_transformation | inferred_mapping | unresolved.
    """
    is_iri = runtime.policy.fields[field_id]["datatype"] == "iri"
    value: dict[str, Any] = {"raw": raw, "candidates_considered": []}
    exact: list[str] = []
    if is_iri:
        exact = _exact_matches(runtime, field_id, raw)
        search = runtime.search_ontology_terms(str(raw), field_id, CANDIDATES_CONSIDERED)
        value["candidates_considered"] = [
            {"iri": c["iri"], "label": c["label"], "score": c["score"]}
            for c in search["candidates"]
        ]
        deterministic = exact[0] if len(exact) == 1 else None
        rule = "exact case-insensitive label or synonym match inside the authorised scope"
    else:
        deterministic = raw
        rule = "value is a member of the field's declared literal value set"
    deterministic_check = None
    if deterministic is not None:
        deterministic_check = runtime.validate_semantic_mapping(
            {"field": field_id, "decision": "map", "value": deterministic}
        )["results"][0]
        if deterministic_check["status"] == "admissible":
            return {
                **value,
                "status": "deterministic_transformation",
                **_resolved(runtime, is_iri, deterministic),
                "rule": rule,
                "validation": {"status": "admissible", "errors": []},
            }
    key = (_source_key(source), field_id, str(raw))
    decision = inferred_by_key.get(key)
    if decision is not None:
        used.add(key)
        result = runtime.validate_semantic_mapping(
            {
                "field": field_id,
                "decision": decision.get("decision", "map"),
                "value": decision.get("value"),
                "reason": decision.get("reason"),
            }
        )["results"][0]
        record = {
            "selector": decision["selector"],
            "proposed": decision.get("value"),
            "decision": decision.get("decision", "map"),
            "rationale": decision.get("reason"),
            "alternatives": decision.get("alternatives", []),
            "validation": {
                "status": result["status"],
                "errors": result["errors"],
                "warnings": result["warnings"],
            },
        }
        if result["status"] == "admissible":
            return {
                **value,
                "status": "inferred_mapping",
                **_resolved(runtime, is_iri, result["canonical_values"][0]),
                "inference": record,
            }
        reason = (
            "selector left it unresolved"
            if result["status"] == "unresolved"
            else "selector proposal failed validation"
        )
        return {**value, "status": "unresolved", "iri": None, "reason": reason, "inference": record}
    if is_iri:
        reason = (
            "several authorised terms match exactly"
            if len(exact) > 1
            else "no authorised term's label or synonym equals the value"
        )
    else:
        reason = "value is not in the field's declared literal value set"
    unresolved = {**value, "status": "unresolved", "iri": None, "reason": reason}
    if len(exact) > 1:
        unresolved["exact_matches"] = exact
    if deterministic_check is not None and not is_iri:
        unresolved["validation"] = {"status": "invalid", "errors": deterministic_check["errors"]}
    return unresolved


def _resolved(runtime: OntologyRuntime, is_iri: bool, resolved: Any) -> dict:
    if is_iri:
        return {"iri": resolved, "label": runtime.snapshot.terms[resolved].get("label")}
    return {"iri": None, "literal": resolved}


def write_semantic_record(
    record: dict,
    output_dir: Path,
    name: str | None = None,
    *,
    source_dir: Path | None = None,
) -> Path:
    """Write the record into ``OUTPUT/semantic/`` only, atomically; returns the path.

    The destination is never an arbitrary path: ``name`` is a plain ``*.json`` file name inside
    the output's own ``semantic/`` directory. A symlinked ``semantic/`` directory or record, a
    non-regular existing file, or a ``semantic/`` directory that resolves into the source tree
    is refused before anything is written. The bytes go to a temporary file in the same directory
    and replace the record with one ``os.replace``, so a reader sees the old or the new record,
    never a partial one, and an ingest output or source file can never be overwritten.
    """
    name = RECORD_FILENAME if name is None else name
    if not isinstance(name, str) or not _RECORD_NAME.match(name):
        raise SemanticError(
            f"record name {name!r} must be a plain *.json file name; the record is always "
            f"written inside OUTPUT/{SEMANTIC_DIRNAME}/"
        )
    output = Path(output_dir)
    if output.is_symlink() or not output.is_dir():
        raise SemanticError(f"{output} is not an ingest output directory")
    output = output.resolve(strict=True)
    if not (output / "manifest.json").is_file():
        raise SemanticError(f"{output} holds no manifest.json; not an ingest output")
    directory = output / SEMANTIC_DIRNAME
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise SemanticError(f"refusing to write through {directory}: not a plain directory")
    if source_dir is not None:
        source = Path(source_dir).resolve()
        if directory.resolve() == source or source in directory.resolve().parents:
            raise SemanticError("refusing to write the semantic record inside the source tree")
    directory.mkdir(exist_ok=True)
    target = directory / name
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise SemanticError(f"refusing to replace {target}: not a regular file")
    payload = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False).encode() + b"\n"
    handle, temporary = tempfile.mkstemp(prefix=".semantic-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    return target


def record_digest(record: dict) -> str:
    return sha256_of(record)


__all__ = [
    "BINDINGS_SCHEMA",
    "ENGINE_VERSION",
    "INFERRED_SCHEMA",
    "RECORD_CONTRACT",
    "RECORD_FILENAME",
    "SEMANTIC_DIRNAME",
    "STATUSES",
    "build_semantic_record",
    "canonical_json",
    "load_bindings",
    "load_inferred",
    "record_digest",
    "write_semantic_record",
]
