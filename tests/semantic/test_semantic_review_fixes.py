"""Regression tests for the review of PR #101 (issue #102) and the tool-isolation item of
Data2AgentBench #7. Each test failed on the reviewed head e81e63d."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from data2agent.cli import main
from data2agent.errors import OutputError, QueryValidationError
from data2agent.ingest.pipeline import ingest
from data2agent.mcp import DatasetService
from data2agent.mcp.service import SEMANTIC_RETRIEVAL_TOOLS
from data2agent.semantic import mapping as semantic_mapping
from data2agent.semantic.constraints import (
    MappingPolicy,
    OntologyRuntime,
    OntologySnapshot,
    SemanticError,
    literal_error,
)
from data2agent.semantic.mapping import (
    build_semantic_record,
    load_bindings,
    load_inferred,
    write_semantic_record,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "semantic"
MUS = "http://purl.obolibrary.org/obo/NCBITaxon_10090"


def _digests(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _semantic_args(output: Path, *extra: str) -> list[str]:
    return [
        "semantic",
        str(output),
        "--snapshot",
        str(EXAMPLE / "snapshot.json"),
        "--policy",
        str(EXAMPLE / "policy.json"),
        "--bindings",
        str(EXAMPLE / "bindings.preclinical-minimal.json"),
        *extra,
    ]


@pytest.fixture
def runtime() -> OntologyRuntime:
    return OntologyRuntime.load(EXAMPLE / "snapshot.json", EXAMPLE / "policy.json")


@pytest.fixture
def workspace(example_dataset, tmp_path):
    source = tmp_path / "source"
    shutil.copytree(example_dataset, source)
    output = tmp_path / "out"
    ingest(source, output)
    return source, output


def _record(output, runtime, source=None, inferred=None):
    bindings, digest = load_bindings(EXAMPLE / "bindings.preclinical-minimal.json")
    loaded, inferred_digest = load_inferred(inferred) if inferred else (None, None)
    return build_semantic_record(
        output,
        runtime,
        bindings,
        bindings_sha256=digest,
        source_dir=source,
        inferred=loaded,
        inferred_sha256=inferred_digest,
    )


# -- D2A-01: the record can only be written inside OUTPUT/semantic/ -----------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../manifest.json",
        "../../source/animals.csv",
        "/tmp/elsewhere.json",
        "a/b.json",
        "x.csv",
        ".hidden.json",
        "",
    ],
)
def test_record_name_cannot_escape_the_semantic_directory(workspace, runtime, name):
    source, output = workspace
    before = (_digests(source), _digests(output))
    with pytest.raises(SemanticError):
        write_semantic_record(_record(output, runtime, source), output, name, source_dir=source)
    assert (_digests(source), _digests(output)) == before


def test_the_old_arbitrary_destination_flag_is_gone(workspace):
    source, output = workspace
    before = _digests(source)
    try:  # argparse may read --record as an abbreviation of --record-name; both must refuse
        status = main(_semantic_args(output, "--record", str(source / "animals.csv")))
    except SystemExit as error:
        status = error.code
    assert status != 0
    assert _digests(source) == before


def test_cli_refuses_a_traversing_record_name(workspace):
    source, output = workspace
    before = (_digests(source), _digests(output))
    assert main(_semantic_args(output, "--record-name", "../../source/animals.csv")) != 0
    assert (_digests(source), _digests(output)) == before


def test_a_symlinked_semantic_directory_into_the_source_is_refused(workspace, runtime):
    source, output = workspace
    (output / "semantic").symlink_to(source, target_is_directory=True)
    before = _digests(source)
    with pytest.raises(SemanticError, match="not a plain directory"):
        write_semantic_record(_record(output, runtime, source), output, source_dir=source)
    assert _digests(source) == before


def test_a_symlinked_record_file_is_refused(workspace, runtime):
    source, output = workspace
    (output / "semantic").mkdir()
    (output / "semantic" / "semantic-mapping.json").symlink_to(source / "animals.csv")
    before = _digests(source)
    with pytest.raises(SemanticError, match="not a regular file"):
        write_semantic_record(_record(output, runtime, source), output, source_dir=source)
    assert _digests(source) == before


def test_a_semantic_directory_inside_the_source_tree_is_refused(workspace, runtime):
    source, output = workspace
    with pytest.raises(SemanticError, match="inside the source tree"):
        write_semantic_record(_record(output, runtime, source), output, source_dir=output)


def test_a_name_equal_to_an_ingest_output_stays_inside_semantic(workspace, runtime):
    source, output = workspace
    manifest = (output / "manifest.json").read_bytes()
    path = write_semantic_record(_record(output, runtime, source), output, "manifest.json")
    assert path == output.resolve() / "semantic" / "manifest.json"
    assert (output / "manifest.json").read_bytes() == manifest


def test_write_is_atomic(workspace, runtime, monkeypatch):
    source, output = workspace
    first = write_semantic_record(_record(output, runtime, source), output, source_dir=source)
    original = first.read_bytes()

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(semantic_mapping.os, "replace", broken_replace)
    with pytest.raises(OSError):
        write_semantic_record({"changed": True}, output, source_dir=source)
    assert first.read_bytes() == original
    assert [p.name for p in first.parent.iterdir()] == ["semantic-mapping.json"]


# -- D2A-02: declared XSD datatypes are enforced ------------------------------------------------


@pytest.mark.parametrize(
    ("datatype", "valid", "invalid"),
    [
        (
            "xsd:date",
            ["2026-02-28", "2024-02-29", "2026-02-28Z", "2026-02-28+01:00"],
            [
                "not-a-date",
                "2026-02-30",
                "2023-02-29",
                "2026-2-28",
                "2026-02-28T00:00:00",
                20260228,
                True,
                "2026-02-28+15:00",
            ],
        ),
        (
            "xsd:dateTime",
            ["2026-02-28T13:45:00", "2026-02-28T13:45:00.25+02:00", "2026-02-28T13:45:00Z"],
            [
                "2026-02-28 13:45:00",
                "2026-02-28T25:00:00",
                "2026-02-28T13:60:00",
                "2026-02-30T00:00:00",
                "2026-02-28T13:45",
                "2026-02-28",
            ],
        ),
        (
            "xsd:integer",
            ["12", "-3", "+4", 7, 0],
            ["not-an-integer", "1.5", 1.5, True, "", " 1", "1e3"],
        ),
        (
            "xsd:decimal",
            ["1.5", ".5", "-2", "3.", 3, 2.5],
            ["1e3", "nan", float("nan"), float("inf"), True, "abc", "", "1,5"],
        ),
        ("xsd:boolean", ["true", "false", "1", "0", True], ["yes", "TRUE", 1, 0.0]),
        ("xsd:string", ["", "x"], [5, 1.0, False]),
        ("literal", ["x", 3, 2.5], [float("nan"), None, {"a": 1}, True]),
    ],
)
def test_literal_datatypes_are_checked_without_coercion(datatype, valid, invalid):
    for value in valid:
        assert literal_error(datatype, value) is None, (datatype, value)
    for value in invalid:
        assert literal_error(datatype, value) is not None, (datatype, value)


def _runtime_with(fields: dict) -> OntologyRuntime:
    snapshot = OntologySnapshot.load(EXAMPLE / "snapshot.json")
    data = json.loads((EXAMPLE / "policy.json").read_text(encoding="utf-8"))
    data["fields"].update(fields)
    return OntologyRuntime(snapshot, MappingPolicy(data, snapshot))


def test_validator_rejects_invalid_typed_literals():
    runtime = _runtime_with(
        {
            "born": {"datatype": "xsd:date", "cardinality": {"min": 1, "max": 1}},
            "count": {"datatype": "xsd:integer", "cardinality": {"min": 1, "max": 1}},
        }
    )
    for field, value in (
        ("born", "not-a-date"),
        ("born", "2026-02-30"),
        ("count", "not-an-integer"),
    ):
        result = runtime.validate_semantic_mapping(
            {"field": field, "decision": "map", "value": value}
        )
        assert result["results"][0]["errors"][0]["code"] == "datatype_mismatch", (field, value)
    ok = runtime.validate_semantic_mapping(
        {"field": "born", "decision": "map", "value": "2024-02-29"}
    )
    assert ok["formally_valid"] is True


def test_policy_value_sets_must_fit_their_datatype():
    with pytest.raises(SemanticError, match="allowed value"):
        _runtime_with({"count": {"datatype": "xsd:integer", "allowed": {"values": ["many"]}}})


# -- D2A-03: the served sidecar is bound to the evidence ledger ---------------------------------


def test_stale_sidecar_is_refused_after_the_ledger_changes(workspace):
    source, output = workspace
    assert main(_semantic_args(output)) == 0
    ledger = output / "evidence.json"
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["claims"] = data["claims"][:-1]
    ledger.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(OutputError, match="evidence ledger"):
        DatasetService(
            output,
            semantic_snapshot=EXAMPLE / "snapshot.json",
            semantic_policy=EXAMPLE / "policy.json",
        )


# -- D2A-04: literal fields go through the same inference flow ----------------------------------


def _licence_workspace(example_dataset, tmp_path, licence: str):
    source = tmp_path / "source"
    shutil.copytree(example_dataset, source)
    description = source / "dataset_description.json"
    data = json.loads(description.read_text(encoding="utf-8"))
    data["License"] = licence
    description.write_text(json.dumps(data, indent=2), encoding="utf-8")
    output = tmp_path / "out"
    ingest(source, output)
    return source, output


def _inferred(tmp_path, value) -> Path:
    path = tmp_path / "inferred.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "d2a-semantic-inferred-1",
                "selector": {"name": "test reviewer", "kind": "human"},
                "decisions": [
                    {
                        "source": {"file": "dataset_description.json", "key": "License"},
                        "field": "license",
                        "raw_value": "CC0",
                        "decision": "map",
                        "value": value,
                        "reason": "reviewer equates the short name with the SPDX id",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _licence_value(record):
    entry = next(e for e in record["entries"] if e["field"] == "license")
    return entry["values"][0]


def test_literal_field_accepts_a_validated_external_inference(example_dataset, tmp_path, runtime):
    source, output = _licence_workspace(example_dataset, tmp_path, "CC0")
    without = _licence_value(_record(output, runtime, source))
    assert without["status"] == "unresolved"
    value = _licence_value(_record(output, runtime, source, _inferred(tmp_path, "CC0-1.0")))
    assert value["status"] == "inferred_mapping"
    assert value["literal"] == "CC0-1.0" and value["raw"] == "CC0"
    assert value["inference"]["selector"]["name"] == "test reviewer"


def test_literal_inference_outside_the_value_set_stays_unresolved(
    example_dataset, tmp_path, runtime
):
    source, output = _licence_workspace(example_dataset, tmp_path, "CC0")
    value = _licence_value(_record(output, runtime, source, _inferred(tmp_path, "MIT")))
    assert value["status"] == "unresolved"
    assert value["inference"]["validation"]["errors"][0]["code"] == "literal_not_allowed"


# -- D2A-05: nulls are rejected, never filtered ---------------------------------------------------


@pytest.mark.parametrize("value", [[MUS, None], [None], None, [], [MUS, 5], [MUS, MUS]])
def test_map_values_are_not_repaired(runtime, value):
    result = runtime.validate_semantic_mapping(
        {"field": "species", "decision": "map", "value": value}
    )
    assert result["results"][0]["status"] == "invalid", value


def test_null_inside_a_map_is_named(runtime):
    result = runtime.validate_semantic_mapping(
        {"field": "species", "decision": "map", "value": [MUS, None]}
    )["results"][0]
    assert result["errors"][0]["code"] == "null_value"
    assert result["errors"][0]["value"] == [MUS, None]


# -- D2A-06: the source JSON read is bounded before it happens ------------------------------------


def test_source_json_read_never_exceeds_the_cap(workspace, runtime, monkeypatch):
    source, output = workspace
    monkeypatch.setattr(semantic_mapping, "_MAX_JSON_BYTES", 16)
    requested: list[int] = []
    real_open = Path.open

    def recording_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self.name != "dataset_description.json":
            return handle

        class Recorder:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                handle.close()

            def read(self_inner, size=-1):
                requested.append(size)
                return handle.read(size)

        return Recorder()

    def forbidden(self):
        raise AssertionError("read_bytes would load the whole file")

    monkeypatch.setattr(Path, "open", recording_open)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    inputs = semantic_mapping._Inputs(output, source)
    fact = inputs.source_fact({"file": "dataset_description.json", "key": "Species"})
    assert fact["status"] == "unresolved" and "exceeds" in fact["reason"]
    assert requested and all(0 < size <= 17 for size in requested)


# -- Data2AgentBench #7 (server side): the semantic tool surface is an explicit allow-list -------


def _service(output, tools=None):
    return DatasetService(
        output,
        semantic_snapshot=EXAMPLE / "snapshot.json",
        semantic_policy=EXAMPLE / "policy.json",
        semantic_tools=tools,
    )


def test_default_attachment_exposes_retrieval_only(workspace):
    _, output = workspace
    assert main(_semantic_args(output)) == 0  # a bound sidecar exists
    service = _service(output)
    exposed = set(service.available_tools())
    assert set(SEMANTIC_RETRIEVAL_TOOLS) <= exposed
    assert not {"validate_semantic_mapping", "get_semantic_mapping"} & exposed
    with pytest.raises(QueryValidationError, match="not exposed"):
        service.validate_semantic_mapping([])
    with pytest.raises(QueryValidationError, match="not exposed"):
        service.get_semantic_mapping()


@pytest.mark.parametrize(
    "tools", [["search_ontology_terms", "run_fair_check"], ["get_term_context", "get_term_context"]]
)
def test_unknown_or_duplicate_semantic_tools_fail(workspace, tools):
    _, output = workspace
    with pytest.raises(OutputError):
        _service(output, tools)


def test_record_tool_needs_a_bound_record_and_tools_need_a_snapshot(workspace):
    _, output = workspace
    with pytest.raises(OutputError, match="bound"):
        _service(output, ["get_semantic_mapping"])
    with pytest.raises(OutputError, match="needs --semantic-snapshot"):
        DatasetService(output, semantic_tools=["search_ontology_terms"])


@pytest.mark.anyio
async def test_mcp_registers_exactly_the_requested_semantic_tools(workspace):
    pytest.importorskip("mcp")
    from data2agent.mcp.server import build_server

    _, output = workspace
    plain = {t.name for t in await build_server(DatasetService(output)).list_tools()}
    narrow = build_server(_service(output, ["search_ontology_terms"]))
    assert {t.name for t in await narrow.list_tools()} - plain == {"search_ontology_terms"}
    with pytest.raises(Exception, match="Unknown tool"):  # noqa: B017 - ToolError's module differs across mcp versions
        await narrow.call_tool("validate_semantic_mapping", {"decisions": []})


# -- digest semantics: canonical JSON for documents, raw bytes for pinned artifacts -------------


def test_snapshot_digest_is_canonical_json_and_artifact_digests_are_raw_bytes():
    raw = (EXAMPLE / "snapshot.json").read_bytes()
    snapshot = OntologySnapshot.load(EXAMPLE / "snapshot.json")
    assert snapshot.sha256 != hashlib.sha256(raw).hexdigest()  # indentation is not identity
    for source in snapshot.data["sources"]:
        artifact = EXAMPLE / source["artifact"]
        assert source["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert os.path.getsize(EXAMPLE / "snapshot.json") > 0
