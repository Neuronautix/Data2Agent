"""The MCP binding.

These tests exercise the real protocol surface in-process: the tools a host
would see, and the results it would get back. The binding must add no behaviour
of its own, so anything asserted here should already be true of the service.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp", reason="the MCP binding needs the 'mcp' extra")

from data2agent.mcp import DatasetService  # noqa: E402
from data2agent.mcp.server import build_server  # noqa: E402


@pytest.fixture
def server(ingested):
    return build_server(DatasetService(ingested.output_dir))


async def _tool_names(server) -> set[str]:
    return {tool.name for tool in await server.list_tools()}


def _template_uris(templates) -> set[str]:
    """The field is uriTemplate in mcp 1.x and uri_template in 2.x."""
    fields = [template.model_dump() for template in templates]
    return {str(field.get("uri_template") or field["uriTemplate"]) for field in fields}


def _text_of(result) -> str:
    """Pull the text payload out of a tool result, across mcp 1.x and 2.x shapes."""
    content = getattr(result, "content", None)
    if content is None:  # mcp 1.x returns (content, structured)
        content = result[0]
    return content[0].text


@pytest.mark.anyio
async def test_structured_mode_registers_the_deterministic_surface(server):
    assert await _tool_names(server) == {
        "dataset_inventory",
        "list_files",
        "inspect_file",
        "inspect_table",
        "list_tables",
        "read_rows",
        "filter_rows",
        "aggregate",
        "aggregate_join",
        "describe_variable",
        "join_tables",
        "list_relationships",
        "get_relationship",
        "join_relationship",
        "get_metadata",
        "get_evidence",
        "get_provenance",
        "resolve_identifier",
    }


@pytest.mark.anyio
async def test_fair_deterministic_mode_adds_the_profile_tools(ingested):
    server = build_server(DatasetService(ingested.output_dir, mode="fair-deterministic"))
    names = await _tool_names(server)
    assert {
        "list_fair_rules",
        "get_fair_indicator",
        "run_fair_check",
        "validate_identifier",
    } <= names


@pytest.mark.anyio
async def test_calling_run_fair_check_over_mcp_returns_an_assessment(ingested):
    server = build_server(DatasetService(ingested.output_dir, mode="fair-deterministic"))
    result = await server.call_tool("run_fair_check", {})
    payload = json.loads(_text_of(result))
    assert payload["profile"]["id"] == "fair"
    assert payload["summary"]["unknown"] >= 2, "unimplemented rules must survive as unknown"


@pytest.mark.anyio
async def test_foundation_items_are_individually_addressable_over_mcp(ingested):
    server = build_server(DatasetService(ingested.output_dir, mode="fair-deterministic"))
    names = await _tool_names(server)
    assert {
        "list_fair_principles",
        "get_fair_principle",
        "assess_fair_principles",
        "get_fair_recommendations",
        "plan_fair_publication",
    } <= names
    listing = json.loads(_text_of(await server.call_tool("list_fair_principles", {})))
    assert len(listing["principles"]) == 15
    detail = json.loads(
        _text_of(await server.call_tool("get_fair_principle", {"principle_id": "A1.2"}))
    )
    assert detail["id"] == "A1.2" and detail["required_evidence"]
    assessment = json.loads(
        _text_of(await server.call_tool("assess_fair_principles", {"principle_id": "F4"}))
    )
    assert assessment["summary"] == {"total": 1, "pass": 0, "unknown": 1}
    assert assessment["results"][0]["principle"] == "F4"
    assert assessment["results"][0]["recommendations"]
    recommendations = json.loads(_text_of(await server.call_tool("get_fair_recommendations", {})))
    assert recommendations["recommendations"]
    assert all(item["findings"] for item in recommendations["recommendations"])
    assert len(recommendations["principle_recommendations"]) == 15
    a2 = next(
        item for item in recommendations["principle_recommendations"] if item["principle"] == "A2"
    )
    assert a2["result"] == "unknown"
    assert len(a2["unverified_requirements"]) == 3
    assert all(need["action"] for need in a2["unverified_requirements"])
    assert assessment["results"][0]["evidence_plan"][1]["action"].startswith("Query")


@pytest.mark.anyio
async def test_opt_in_publication_probe_is_available_over_mcp(ingested, monkeypatch):
    url = "https://doi.org/10.5281/zenodo.0000000"
    monkeypatch.setattr(
        "data2agent.profiles.fair.live.probe_public_url",
        lambda value, **kwargs: {
            "method": "bounded-public-http-get",
            "source": value,
            "observed_at": "2026-09-30T12:00:00Z",
            "result": "reached",
            "final_url": "https://repository.example/record",
            "status_code": 200,
            "identifier_seen_in_sample": True,
            "chain": [],
        },
    )
    server = build_server(DatasetService(ingested.output_dir, mode="fair-deterministic"))
    result = await server.call_tool(
        "assess_fair_principles",
        {"principle_id": "A1.1", "live": True, "publication_url": url},
    )
    assessment = json.loads(_text_of(result))
    assert assessment["publication_binding"] is True
    assert assessment["results"][0]["result"] == "unknown"
    assert assessment["results"][0]["evidence_plan"][0]["status"] == "observed_partial"


@pytest.mark.anyio
async def test_unpublished_guidance_is_available_over_mcp(ingested):
    server = build_server(DatasetService(ingested.output_dir, mode="fair-deterministic"))
    result = await server.call_tool("get_fair_recommendations", {"unpublished": True})
    guidance = json.loads(_text_of(result))
    f4 = next(item for item in guidance["principle_recommendations"] if item["principle"] == "F4")
    assert any(need["status"] == "pending_publication" for need in f4["unverified_requirements"])


@pytest.mark.anyio
async def test_publication_advice_is_available_over_mcp(ingested):
    server = build_server(DatasetService(ingested.output_dir, mode="fair-deterministic"))
    result = await server.call_tool(
        "plan_fair_publication", {"purpose": "test", "test_with_real_data": False}
    )
    advice = json.loads(_text_of(result))
    assert advice["recommended_path"] == "sandbox_test"
    assert advice["external_action_taken"] is False


@pytest.mark.anyio
async def test_calling_get_provenance_over_mcp_returns_the_timestamps(server):
    result = await server.call_tool("get_provenance", {})
    payload = json.loads(_text_of(result))
    assert payload["ingested_at"].endswith("Z")
    assert payload["duration_seconds"] >= 0


@pytest.mark.anyio
async def test_raw_mode_registers_only_two_tools(ingested):
    server = build_server(DatasetService(ingested.output_dir, mode="raw"))
    assert await _tool_names(server) == {"list_files", "inspect_file"}


@pytest.mark.anyio
async def test_every_tool_documents_itself(server):
    for tool in await server.list_tools():
        assert tool.description and len(tool.description) > 30, (
            f"{tool.name} needs a usable docstring"
        )


@pytest.mark.anyio
async def test_calling_inspect_table_over_mcp_returns_the_profile(server):
    result = await server.call_tool("inspect_table", {"path": "animals.csv"})
    payload = json.loads(_text_of(result))
    assert payload["rows"] == 48
    assert payload["missing"]["sex"] == 12


@pytest.mark.anyio
async def test_tool_input_schemas_are_closed(server):
    for tool in await server.list_tools():
        schema = tool.model_dump(by_alias=True)["inputSchema"]
        assert schema.get("additionalProperties") is False


@pytest.mark.anyio
async def test_unknown_tool_argument_is_rejected_before_execution(server):
    try:
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:
        from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match=r"unknown argument.*filters.*allowed arguments"):
        await server.call_tool(
            "read_rows",
            {
                "path": "observations.csv",
                "columns": ["latency_s"],
                "filters": [{"column": "animal_id", "op": "eq", "value": "A002"}],
            },
        )


@pytest.mark.anyio
async def test_row_tool_descriptions_distinguish_slicing_from_filtering(server):
    tools = {tool.name: tool for tool in await server.list_tools()}

    read_description = " ".join((tools["read_rows"].description or "").split())
    filter_description = " ".join((tools["filter_rows"].description or "").split())

    assert "offset/limit only" in read_description
    assert "does not apply predicates" in read_description
    assert "use filter_rows instead" in read_description

    assert "must satisfy one or more conditions" in filter_description
    assert "applies the supplied predicates" in filter_description
    assert "Unlike read_rows" in filter_description


@pytest.mark.anyio
async def test_calling_read_rows_over_mcp_returns_observations(server):
    result = await server.call_tool(
        "read_rows",
        {"path": "animals.csv", "columns": ["animal_id", "weight_g"], "limit": 2},
    )
    payload = json.loads(_text_of(result))
    assert payload["returned"] == 2
    assert payload["rows"][0]["source_row"] == 2
    assert isinstance(payload["rows"][0]["values"]["weight_g"], int)


@pytest.mark.anyio
async def test_calling_filter_rows_over_mcp_selects_a_group(server):
    result = await server.call_tool(
        "filter_rows",
        {
            "path": "animals.csv",
            "filters": [{"column": "genotype", "op": "eq", "value": "KO"}],
            "columns": ["animal_id", "genotype"],
            "limit": 2,
        },
    )
    payload = json.loads(_text_of(result))
    assert payload["returned"] == 2
    assert payload["matches_in_scanned_rows"] == 24
    assert all(row["values"]["genotype"] == "KO" for row in payload["rows"])


@pytest.mark.anyio
async def test_query_validation_error_is_readable_over_mcp(server):
    try:
        result = await server.call_tool(
            "filter_rows",
            {
                "path": "animals.csv",
                "filters": [{"column": "session", "op": "eq", "value": 2}],
                "columns": ["animal_id", "session"],
                "limit": 2,
            },
        )
    except Exception as error:
        # SDK 2.x raises anticipated tool failures for in-process calls;
        # SDK 1.x returns an isError content result. Both must expose the cause.
        try:
            from mcp.server.mcpserver.exceptions import ToolError
        except ImportError:
            from mcp.server.fastmcp.exceptions import ToolError
        assert isinstance(error, ToolError)
        assert "unknown column(s) for 'animals.csv'" in str(error)
        assert "session" in str(error)
        assert "available columns" in str(error)
        return

    is_error = getattr(result, "is_error", None)
    if is_error is not None:
        assert is_error is True

    text = _text_of(result)
    assert "unknown column(s) for 'animals.csv'" in text
    assert "session" in text
    assert "available columns" in text


@pytest.mark.anyio
async def test_calling_aggregate_over_mcp_returns_group_statistics(server):
    result = await server.call_tool(
        "aggregate",
        {
            "path": "animals.csv",
            "group_by": ["genotype"],
            "metrics": [
                {"op": "count", "name": "n"},
                {"op": "mean", "column": "weight_g", "name": "mean_weight_g"},
            ],
        },
    )
    payload = json.loads(_text_of(result))
    groups = {item["group"]["genotype"]: item["metrics"] for item in payload["groups"]}
    assert groups["KO"]["n"] == 24
    assert groups["WT"]["n"] == 24
    contributor = payload["provenance"]["inputs"][0]
    assert contributor["complete"] is True
    assert contributor["backing_file"] == "animals.csv"
    assert contributor["included_source_row_ranges"] == [[2, 49]]


@pytest.mark.anyio
async def test_calling_aggregate_join_over_mcp_counts_units_per_registry_group(server):
    result = await server.call_tool(
        "aggregate_join",
        {
            "left": "animals.csv",
            "right": "observations.csv",
            "left_keys": ["animal_id"],
            "right_keys": ["animal_id"],
            "group_by": ["left.genotype"],
            "unit": ["right.animal_id"],
            "unit_metrics": [{"op": "mean", "column": "right.latency_s"}],
            "metrics": [
                {"op": "count", "name": "n_animals"},
                {"op": "sem", "column": "mean:right.latency_s"},
            ],
        },
    )
    payload = json.loads(_text_of(result))
    groups = {item["group"]["left.genotype"]: item for item in payload["groups"]}
    assert groups["KO"]["metrics"]["n_animals"] == 24
    assert groups["KO"]["n_units"] == 24 and groups["KO"]["n_rows"] == 72
    assert payload["join"]["cardinality"] == "one_to_many"


@pytest.mark.anyio
async def test_relationships_are_explicitly_undetermined_over_mcp(server):
    result = await server.call_tool("list_relationships", {})
    payload = json.loads(_text_of(result))
    assert payload["determined"] is False
    assert payload["relationships"] == []


@pytest.mark.anyio
async def test_resources_are_listed_and_readable(server):
    uris = {str(resource.uri) for resource in await server.list_resources()}
    assert {"dataset://manifest", "dataset://evidence", "dataset://provenance"} <= uris
    contents = await server.read_resource("dataset://manifest")
    assert json.loads(list(contents)[0].content)["file_count"] == 4


@pytest.mark.anyio
async def test_raw_mode_registers_no_structured_resource(ingested):
    """The binding must gate resources by mode too, or a raw client can simply
    enumerate dataset://manifest and read the structured condition."""
    server = build_server(DatasetService(ingested.output_dir, mode="raw"))
    uris = {str(resource.uri) for resource in await server.list_resources()}
    assert "dataset://manifest" not in uris
    assert "dataset://evidence" not in uris

    templates = _template_uris(await server.list_resource_templates())
    assert "dataset://files/{path}" in templates


@pytest.mark.anyio
async def test_structured_mode_registers_the_structured_resources(server):
    uris = {str(resource.uri) for resource in await server.list_resources()}
    assert {
        "dataset://manifest",
        "dataset://provenance",
        "dataset://evidence",
        "dataset://metadata",
        "dataset://relationships",
    } <= uris
