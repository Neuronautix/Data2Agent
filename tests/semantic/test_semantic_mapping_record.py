"""The optional semantic record: evidence-linked, status-separated, never mutating inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from data2agent.cli import main
from data2agent.semantic.constraints import OntologyRuntime, SemanticError
from data2agent.semantic.mapping import (
    RECORD_FILENAME,
    SEMANTIC_DIRNAME,
    build_semantic_record,
    load_bindings,
    load_inferred,
    write_semantic_record,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "semantic"
MUS = "http://purl.obolibrary.org/obo/NCBITaxon_10090"
MALE = "http://purl.obolibrary.org/obo/PATO_0000384"


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def runtime() -> OntologyRuntime:
    return OntologyRuntime.load(EXAMPLE / "snapshot.json", EXAMPLE / "policy.json")


def _record(ingested, runtime, inferred=None):
    bindings, digest = load_bindings(EXAMPLE / "bindings.preclinical-minimal.json")
    loaded, inferred_digest = load_inferred(inferred) if inferred else (None, None)
    return build_semantic_record(
        ingested.output_dir,
        runtime,
        bindings,
        bindings_sha256=digest,
        source_dir=ingested.source_dir if hasattr(ingested, "source_dir") else None,
        inferred=loaded,
        inferred_sha256=inferred_digest,
    )


def _values(record: dict) -> dict[tuple[str, str], dict]:
    return {(e["source_key"], str(v["raw"])): v for e in record["entries"] for v in e["values"]}


def test_statuses_are_kept_apart(ingested, runtime):
    record = _record(ingested, runtime)
    values = _values(record)
    species = values[("dataset_description.json#Species", "Mus musculus")]
    assert species["status"] == "deterministic_transformation" and species["iri"] == MUS
    assert (
        values[("dataset_description.json#License", "CC0-1.0")]["status"]
        == "deterministic_transformation"
    )
    for raw in ("M", "F"):  # codes without a legend: no lookup resolves them
        sex = values[("animals.csv:sex", raw)]
        assert sex["status"] == "unresolved" and sex["iri"] is None
    assert record["summary"]["inferred_mapping"] == 0


def test_every_value_cites_ledger_claims_and_the_checksum(ingested, runtime):
    record = _record(ingested, runtime)
    evidence = json.loads((ingested.output_dir / "evidence.json").read_text(encoding="utf-8"))
    claim_ids = {c["claim_id"] for c in evidence["claims"]}
    manifest = json.loads((ingested.output_dir / "manifest.json").read_text(encoding="utf-8"))
    checksums = {f["path"]: f["sha256"] for f in manifest["files"]}
    for entry in record["entries"]:
        fact = entry["source_fact"]
        assert fact["status"] == "verified_source_fact"
        assert fact["evidence_claim_ids"] and set(fact["evidence_claim_ids"]) <= claim_ids
        subject = entry["source"].get("table") or entry["source"]["file"]
        assert fact["source_sha256"] == checksums[subject]


def test_provenance_binding(ingested, runtime):
    record = _record(ingested, runtime)
    manifest_bytes = (ingested.output_dir / "manifest.json").read_bytes()
    assert record["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert record["engine"]["snapshot"]["sha256"] == runtime.snapshot.sha256
    assert record["engine"]["policy"]["sha256"] == runtime.policy.sha256
    assert record["bindings_sha256"] and "not evidence" in record["note"]


def test_inferred_mappings_are_validated_and_labelled(ingested, runtime, tmp_path):
    record = _record(ingested, runtime, EXAMPLE / "inferred.example.json")
    male = _values(record)[("animals.csv:sex", "M")]
    assert male["status"] == "inferred_mapping" and male["iri"] == MALE
    assert male["inference"]["selector"]["name"] == "example-hand-written"
    bad = json.loads((EXAMPLE / "inferred.example.json").read_text(encoding="utf-8"))
    bad["decisions"][0]["value"] = MUS  # a taxon is not a sex
    bad["decisions"].append(
        {
            "source": {"table": "animals.csv", "column": "sex"},
            "field": "sex",
            "raw_value": "X",
            "decision": "map",
            "value": MALE,
        }
    )
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    record = _record(ingested, runtime, path)
    male = _values(record)[("animals.csv:sex", "M")]
    assert male["status"] == "unresolved"
    assert male["inference"]["validation"]["errors"][0]["code"] == "outside_scope"
    # an inference about a value the source does not hold is rejected, not recorded
    assert record["rejected_inferences"][0]["raw_value"] == "X"


def test_semantic_mode_never_mutates_manifest_evidence_or_source(
    ingested, example_dataset, tmp_path
):
    before_out = _tree_digest(ingested.output_dir)
    before_src = _tree_digest(example_dataset)
    args = [
        "semantic",
        str(ingested.output_dir),
        "--snapshot",
        str(EXAMPLE / "snapshot.json"),
        "--policy",
        str(EXAMPLE / "policy.json"),
        "--bindings",
        str(EXAMPLE / "bindings.preclinical-minimal.json"),
        "--inferred",
        str(EXAMPLE / "inferred.example.json"),
    ]
    assert main(args) == 0
    after_out = _tree_digest(ingested.output_dir)
    assert _tree_digest(example_dataset) == before_src
    new = set(after_out) - set(before_out)
    assert new == {f"{SEMANTIC_DIRNAME}/{RECORD_FILENAME}"}
    assert {k: after_out[k] for k in before_out} == before_out
    first = (ingested.output_dir / SEMANTIC_DIRNAME / RECORD_FILENAME).read_bytes()
    assert main(args) == 0  # deterministic: no timestamps, identical bytes
    assert (ingested.output_dir / SEMANTIC_DIRNAME / RECORD_FILENAME).read_bytes() == first


def test_record_cannot_overwrite_ingest_outputs(ingested, runtime):
    record = _record(ingested, runtime)
    with pytest.raises(SemanticError, match="plain"):
        write_semantic_record(record, ingested.output_dir, "../manifest.json")


def test_drifted_source_is_not_read(ingested, runtime, tmp_path, example_dataset):
    import shutil

    drifted = tmp_path / "drifted"
    shutil.copytree(example_dataset, drifted)
    path = drifted / "dataset_description.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace("Mus musculus", "Homo sapiens"), encoding="utf-8"
    )
    bindings, digest = load_bindings(EXAMPLE / "bindings.preclinical-minimal.json")
    record = build_semantic_record(
        ingested.output_dir, runtime, bindings, bindings_sha256=digest, source_dir=drifted
    )
    species = next(e for e in record["entries"] if e["field"] == "species")
    assert species["source_fact"]["status"] == "unresolved"
    assert "checksum" in species["source_fact"]["reason"] and species["values"] == []


def test_unknown_policy_field_in_bindings_is_refused(ingested, runtime, tmp_path):
    path = tmp_path / "b.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "d2a-semantic-bindings-1",
                "bindings": [
                    {"source": {"table": "animals.csv", "column": "strain"}, "field": "strain"}
                ],
            }
        ),
        encoding="utf-8",
    )
    bindings, digest = load_bindings(path)
    with pytest.raises(SemanticError, match="policy"):
        build_semantic_record(ingested.output_dir, runtime, bindings, bindings_sha256=digest)


def test_compiled_example_snapshot_is_current(tmp_path):
    from data2agent.semantic.compile import main as compile_main

    assert (
        compile_main(
            [str(EXAMPLE / "build.json"), "--out", str(EXAMPLE / "snapshot.json"), "--check"]
        )
        == 0
    )


def test_compile_refuses_an_artifact_that_is_not_the_pinned_bytes(tmp_path):
    import shutil

    from data2agent.semantic.compile import build_snapshot

    copy = tmp_path / "semantic"
    shutil.copytree(EXAMPLE, copy)
    (copy / "sources" / "pato-extract.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SemanticError, match="not the pinned"):
        build_snapshot(copy / "build.json")
