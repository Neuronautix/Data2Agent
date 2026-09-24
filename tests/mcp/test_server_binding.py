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
    } <= uris
