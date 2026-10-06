"""Published nested filters and anticipated errors use the same query contract."""

import copy
import json

import pytest

pytest.importorskip("mcp")
from jsonschema import Draft202012Validator  # noqa: E402

from data2agent.errors import QueryLookupError, QueryValidationError  # noqa: E402
from data2agent.mcp import DatasetService  # noqa: E402
from data2agent.mcp.server import build_server  # noqa: E402
from data2agent.query.operations import FILTER_OPERATORS, _validate_filters  # noqa: E402


def schema_of(tool):
    value = tool.model_dump(by_alias=True)
    return value.get("inputSchema") or value["input_schema"]


async def error_text(server, name, arguments):
    try:
        result = await server.call_tool(name, arguments)
    except Exception as error:
        return str(error)
    content = getattr(result, "content", None)
    return content[0].text if content is not None else result[0][0].text


@pytest.mark.anyio
async def test_all_filter_tools_publish_closed_typed_predicates(ingested):
    server = build_server(DatasetService(ingested.output_dir))
    tools = {t.name: t for t in await server.list_tools()}
    for name in ("filter_rows", "aggregate", "aggregate_join"):
        schema = schema_of(tools[name])
        filters = schema["properties"]["filters"]
        array = next((v for v in filters.get("anyOf", []) if v.get("type") == "array"), filters)
        item = array["items"]
        assert item["additionalProperties"] is False
        assert set(item["properties"]["op"]["enum"]) == FILTER_OPERATORS
        check = Draft202012Validator(item)
        assert check.is_valid({"column": "group", "op": "eq", "value": "A"})
        assert check.is_valid({"column": "value", "op": "is_missing"})
        for bad in (
            {"column": "group", "operator": "==", "value": "A"},
            {"column": "group", "op": "equals", "value": "A"},
            {"column": "group", "op": "eq", "value": "A", "extra": True},
            {"column": 3, "op": "eq", "value": "A"},
            {"column": "group", "op": "eq"},
            {"column": "group", "op": "in", "value": "A"},
        ):
            assert not check.is_valid(bad)


@pytest.mark.anyio
async def test_bad_filter_is_visible_unchanged_and_never_reaches_service(ingested, monkeypatch):
    service = DatasetService(ingested.output_dir)
    server = build_server(service)
    calls = []
    monkeypatch.setattr(service, "filter_rows", lambda *a, **kw: calls.append(kw))
    arguments = {
        "path": "animals.csv",
        "filters": [{"column": "genotype", "operator": "==", "value": "KO"}],
    }
    original = copy.deepcopy(arguments)
    message = await error_text(server, "filter_rows", arguments)
    assert "unknown keys" in message and "operator" in message and "op" in message
    assert arguments == original and not calls


@pytest.mark.anyio
async def test_value_and_aggregate_validation_details_reach_the_caller(ingested):
    server = build_server(DatasetService(ingested.output_dir))
    message = await error_text(
        server,
        "filter_rows",
        {"path": "animals.csv", "filters": [{"column": "genotype", "op": "in", "value": "KO"}]},
    )
    assert "requires a list value" in message
    message = await error_text(
        server,
        "aggregate",
        {
            "path": "animals.csv",
            "metrics": [{"op": "mean", "column": "weight_g"}],
            "unit": ["genotype"],
        },
    )
    assert "requires a known column" in message


@pytest.mark.anyio
async def test_valid_filter_still_preserves_results_and_predicates(ingested):
    server = build_server(DatasetService(ingested.output_dir))
    args = {"path": "animals.csv", "filters": [{"column": "genotype", "op": "eq", "value": "KO"}]}
    original = copy.deepcopy(args)
    result = await server.call_tool("filter_rows", args)
    content = getattr(result, "content", None)
    body = json.loads(content[0].text if content is not None else result[0][0].text)
    assert body["matches_in_scanned_rows"] == 24
    assert args == original


def test_core_contract_rejects_bad_shapes_and_remains_value_error_compatible():
    for value in (None, [None], [{"column": "x", "op": []}], [{"column": "x", "op": "eq"}]):
        with pytest.raises(QueryValidationError) as error:
            _validate_filters(value)
        assert isinstance(error.value, ValueError)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        (
            "filter_rows",
            {"path": "animals.csv", "filters": [], "limit": 0},
            "limit must be at least 1",
        ),
        ("read_rows", {"path": "animals.csv", "offset": -1}, "offset must be zero or greater"),
        ("read_rows", {"path": "animals.csv", "limit": 0}, "limit must be at least 1"),
        (
            "aggregate_join",
            {
                "left": "animals.csv",
                "right": "observations.csv",
                "left_keys": ["animal_id"],
                "right_keys": ["animal_id"],
                "how": "cross",
                "metrics": [{"op": "count"}],
            },
            "unsupported join type 'cross'",
        ),
        (
            "aggregate_join",
            {"left": "animals.csv", "metrics": [{"op": "count"}]},
            "aggregate_join needs relationship_id",
        ),
        (
            "join_tables",
            {
                "left": "animals.csv",
                "right": "observations.csv",
                "left_keys": [],
                "right_keys": [],
            },
            "left_keys and right_keys must be non-empty",
        ),
        ("read_rows", {"path": "missing.csv"}, "'missing.csv' was not profiled as a table"),
    ],
)
async def test_anticipated_argument_errors_reach_the_client(ingested, tool, arguments, expected):
    server = build_server(DatasetService(ingested.output_dir))
    text = await error_text(server, tool, arguments)
    assert expected in text


@pytest.mark.anyio
async def test_missing_required_argument_is_named(ingested):
    server = build_server(DatasetService(ingested.output_dir))
    text = await error_text(server, "filter_rows", {"path": "animals.csv"})
    assert "missing required argument(s) for tool 'filter_rows'" in text
    assert "filters" in text


@pytest.mark.anyio
async def test_metric_schemas_are_closed_and_unknown_metric_keys_are_rejected(ingested):
    server = build_server(DatasetService(ingested.output_dir))
    tools = {t.name: t for t in await server.list_tools()}
    for name in ("aggregate", "aggregate_join"):
        properties = schema_of(tools[name])["properties"]
        for key in ("metrics", "unit_metrics"):
            item = next(
                (v for v in properties[key].get("anyOf", []) if v.get("type") == "array"),
                properties[key],
            )["items"]
            assert item["additionalProperties"] is False
            assert set(item["properties"]) == {"op", "column", "name"}

    text = await error_text(
        server,
        "aggregate",
        {"path": "animals.csv", "metrics": [{"op": "count", "bogus": 1}]},
    )
    assert "metric 0 has unknown keys ['bogus']" in text


@pytest.mark.anyio
async def test_closed_schemas_are_built_once_and_stay_stable(ingested):
    server = build_server(DatasetService(ingested.output_dir))
    first = {t.name: copy.deepcopy(schema_of(t)) for t in await server.list_tools()}
    await error_text(server, "read_rows", {"path": "animals.csv", "limit": 0})
    second = {t.name: schema_of(t) for t in await server.list_tools()}
    assert first == second


def test_unknown_column_stays_a_key_error_and_a_value_error(ingested):
    service = DatasetService(ingested.output_dir)
    with pytest.raises(QueryLookupError) as caught:
        service.read_rows("animals.csv", columns=["nope"])
    error = caught.value
    assert isinstance(error, KeyError)
    assert isinstance(error, ValueError)
    assert str(error).startswith("unknown column(s) for 'animals.csv'")
    assert not str(error).startswith("'")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool,arguments,expected",
    [
        ("describe_variable", {"path": "animals.csv", "column": "bogus"}, "available columns"),
        (
            "aggregate_join",
            {"relationship_id": "bogus", "metrics": [{"op": "count"}]},
            "relationships have not been determined",
        ),
    ],
)
async def test_query_lookup_diagnostics_remain_visible(ingested, tool, arguments, expected):
    server = build_server(DatasetService(ingested.output_dir))
    assert expected in await error_text(server, tool, arguments)


@pytest.mark.anyio
@pytest.mark.parametrize("op", [[], {}, None, 4])
@pytest.mark.parametrize("unit_stage", [False, True])
async def test_malformed_metric_operator_is_actionable(ingested, op, unit_stage):
    service = DatasetService(ingested.output_dir)
    arguments = {"path": "animals.csv", "metrics": [{"op": op}]}
    if unit_stage:
        arguments.update(unit=["animal_id"], unit_metrics=[{"op": op}], metrics=[{"op": "count"}])
    original = copy.deepcopy(arguments)
    server = build_server(service)
    assert "unsupported op" in await error_text(server, "aggregate", arguments)
    assert arguments == original


@pytest.mark.anyio
async def test_schema_consumers_cannot_change_cached_dispatch(ingested):
    from data2agent.mcp.server import _tool_input_schema

    server = build_server(DatasetService(ingested.output_dir))
    first = next(t for t in await server.list_tools() if t.name == "read_rows")
    _tool_input_schema(first)["properties"].clear()
    second = next(t for t in await server.list_tools() if t.name == "read_rows")
    assert "path" in _tool_input_schema(second)["properties"]
    text = await error_text(server, "read_rows", {"path": "animals.csv", "bogus": 1})
    assert "unknown argument" in text and "path" in text
