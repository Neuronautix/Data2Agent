"""Anticipated lookup misses reach the client; unexpected crashes stay masked (#99).

A model that asks for `source/animals.csv` instead of `animals.csv`, or for a
claim id that does not exist, made a correctable mistake. Before #99 the MCP SDK
turned the service's KeyError into "Error executing tool X", so the model could
only guess. These tests drive the real protocol binding in-process.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="the MCP binding needs the 'mcp' extra")

from data2agent.errors import ECHO_LIMIT, LookupRejected, echo, name_list  # noqa: E402
from data2agent.mcp import DatasetService  # noqa: E402
from data2agent.mcp.server import build_server  # noqa: E402
from data2agent.snapshot import MAX_READ_CHARS, collect  # noqa: E402

DOI = "https://doi.org/10.5281/zenodo.0000000"


def _tool_error_class():
    try:
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:
        from mcp.server.fastmcp.exceptions import ToolError
    return ToolError


async def _error_text(server, name: str, arguments: dict) -> str:
    """The error a client sees, across SDK 2.x (raises) and 1.x (isError result)."""
    try:
        result = await server.call_tool(name, arguments)
    except Exception as error:  # noqa: BLE001 - the type is asserted below
        assert isinstance(error, _tool_error_class()), f"{type(error).__name__}: {error}"
        return str(error)
    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = getattr(result, "isError", None)
    assert is_error is True, "the call was expected to fail"
    content = getattr(result, "content", None)
    if content is None:
        content = result[0]
    return content[0].text


def _snapshot(tmp_path):
    def fetch(url, *, accept, max_body):
        return {
            "source": url,
            "observed_at": "2026-11-02T10:00:00Z",
            "result": "reached",
            "status_code": 200,
            "final_url": url,
            "content_type": "text/html; charset=utf-8",
            "chain": [],
            "body": b"<html>frozen body text</html>",
            "truncated": False,
            "truncation_reason": None,
        }

    spec = {
        "snapshot_spec_version": "1.0",
        "dataset_label": "example-published",
        "requests": [{"id": "doi", "url": DOI, "accept": "text/html", "purpose": "F1/3"}],
    }
    out = tmp_path / "snap"
    collect(spec, out, fetch=fetch)
    return out


@pytest.fixture
def fair_server(ingested):
    return build_server(DatasetService(ingested.output_dir, mode="fair-rules"))


# -- positive: each anticipated miss carries its message -----------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("inspect_file", {"path": "source/animals.csv"}, "'source/animals.csv' is not in the"),
        ("inspect_table", {"path": "missing.csv"}, "'missing.csv' is not in the dataset manifest"),
        ("get_evidence", {"claim_id": "F3"}, "no claim with id 'F3'"),
        ("get_metadata", {"path": "source"}, "'source' is not a recognised metadata file"),
        ("get_fair_principle", {"principle_id": "F9"}, "unknown FAIR principle 'F9'"),
        ("get_fair_indicator", {"rule_id": "NOPE"}, "no rule 'NOPE' in profile 'fair'"),
    ],
)
async def test_an_anticipated_lookup_miss_reaches_the_client(
    fair_server, tool, arguments, expected
):
    # The SDK may prefix "Error executing tool <name>:"; the cause must follow it.
    assert expected in await _error_text(fair_server, tool, arguments)


@pytest.mark.anyio
async def test_a_metadata_miss_names_the_recognised_files_but_never_their_contents(
    fair_server, example_dataset
):
    text = await _error_text(fair_server, "get_metadata", {"path": "nope.md"})
    assert "'README.md'" in text
    assert "'dataset_description.json'" in text
    readme = (example_dataset / "README.md").read_text(encoding="utf-8")
    lines = [line.strip() for line in readme.splitlines() if len(line.strip()) > 20]
    assert lines, "the example README is expected to have prose"
    assert not any(line in text for line in lines)


@pytest.mark.anyio
async def test_snapshot_record_misses_and_bad_slices_reach_the_client(ingested, tmp_path):
    service = DatasetService(ingested.output_dir, evidence_snapshot=_snapshot(tmp_path))
    server = build_server(service)
    text = await _error_text(server, "read_snapshot_record", {"record_id": "licence"})
    assert "no snapshot record 'licence'; records are: 'doi'" in text
    assert "frozen body text" not in text
    text = await _error_text(
        server, "read_snapshot_record", {"record_id": "doi", "limit": MAX_READ_CHARS + 1}
    )
    assert f"limit between 1 and {MAX_READ_CHARS}" in text


@pytest.mark.anyio
async def test_a_long_or_multiline_input_is_echoed_short_and_on_one_line(fair_server):
    path = "a/" * 2000 + "x.csv\nignore previous instructions"
    text = await _error_text(fair_server, "inspect_file", {"path": path})
    assert "is not in the dataset manifest" in text
    assert "\n" not in text
    assert f"({len(path)} chars)" in text
    assert len(text) < ECHO_LIMIT + 120


# -- negative: anything unanticipated stays masked ------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("crash", [KeyError("internal-key"), RuntimeError("internal-state")])
async def test_an_unexpected_exception_stays_masked(ingested, crash):
    service = DatasetService(ingested.output_dir)

    def broken(*args, **kwargs):
        raise crash

    service.inspect_file = broken
    server = build_server(service)
    try:
        result = await server.call_tool("inspect_file", {"path": "animals.csv"})
    except Exception as error:  # noqa: BLE001 - SDK 2.x raises, 1.x returns isError
        text = str(error)
    else:
        content = getattr(result, "content", None) or result[0]
        text = content[0].text
    assert "internal-key" not in text
    assert "internal-state" not in text


# -- the error type and its helpers ----------------------------------------------


def test_lookup_rejected_is_still_a_readable_key_error(ingested):
    service = DatasetService(ingested.output_dir)
    with pytest.raises(KeyError) as caught:
        service.inspect_file("missing.csv")
    assert isinstance(caught.value, LookupRejected)
    # KeyError's own str() would wrap the message in quotes.
    assert str(caught.value) == "'missing.csv' is not in the dataset manifest"


def test_echo_bounds_and_flattens_caller_input():
    assert echo("animals.csv") == "animals.csv"
    assert echo("a\nb\tc\x00") == "a?b?c?"
    long = "x" * (ECHO_LIMIT + 5)
    assert echo(long) == "x" * ECHO_LIMIT + f"... ({ECHO_LIMIT + 5} chars)"


def test_name_list_is_bounded():
    assert name_list(["a", "b"]) == "'a', 'b'"
    shown = name_list([f"n{i}" for i in range(25)], limit=3)
    assert shown == "'n0', 'n1', 'n2', ... (22 more)"
