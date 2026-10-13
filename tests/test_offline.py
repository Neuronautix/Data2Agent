"""The core runs offline (#87).

``tests/conftest.py`` blocks every non-loopback connection and name lookup for
the whole suite. These tests check that the block itself works, including when
the code under test swallows the failure, and that ingest plus the MCP read and
query surface complete with it in place. The only code that can open a
connection is ``profiles/fair/live.py``, and only on explicit request; see
``docs/offline.md``.
"""

from __future__ import annotations

import json
import socket
import urllib.request

import pytest

from data2agent.mcp import DatasetService

# A reserved documentation address (RFC 5737): never routable.
_OUTSIDE_V4 = ("192.0.2.1", 80)
# The message of conftest.NetworkBlocked.
_BLOCKED = "tried to reach the network"


def test_the_guard_blocks_outbound_connections_and_lookups(network_guard):
    with pytest.raises(AssertionError, match=_BLOCKED):
        socket.create_connection(_OUTSIDE_V4, timeout=1)
    with pytest.raises(AssertionError, match=_BLOCKED):
        socket.getaddrinfo("example.org", 443)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError, match=_BLOCKED):
            sock.connect_ex(_OUTSIDE_V4)
    finally:
        sock.close()
    assert len(network_guard.attempts) == 3
    network_guard.attempts.clear()


def test_a_swallowed_attempt_is_still_recorded(network_guard):
    # What an accidental fetch added under src/ would look like, including code
    # that catches broad exceptions: the attempt is recorded either way, and the
    # fixture's teardown fails any test that leaves one behind.
    try:
        urllib.request.urlopen("https://example.org/schema.json", timeout=1)
    except Exception:
        pass
    assert network_guard.attempts == ["getaddrinfo 'example.org'"]
    network_guard.attempts.clear()


def test_loopback_stays_available():
    # asyncio's self-pipe on Windows is a loopback socket pair.
    left, right = socket.socketpair()
    left.close()
    right.close()


def _payload(result) -> dict:
    content = getattr(result, "content", None)
    if content is None:  # mcp 1.x returns (content, structured)
        content = result[0]
    return json.loads(content[0].text)


@pytest.mark.anyio
async def test_ingest_and_the_mcp_query_surface_run_offline(ingested, network_guard):
    pytest.importorskip("mcp", reason="the MCP binding needs the 'mcp' extra")
    from data2agent.mcp.server import build_server

    server = build_server(DatasetService(ingested.output_dir))
    calls = [
        ("dataset_inventory", {}),
        ("list_tables", {}),
        ("read_rows", {"path": "animals.csv", "limit": 2}),
        (
            "filter_rows",
            {
                "path": "animals.csv",
                "filters": [{"column": "genotype", "op": "eq", "value": "KO"}],
                "limit": 2,
            },
        ),
        (
            "aggregate",
            {
                "path": "animals.csv",
                "group_by": ["genotype"],
                "metrics": [{"op": "mean", "column": "weight_g", "name": "mean_weight_g"}],
            },
        ),
        (
            "aggregate_join",
            {
                "left": "animals.csv",
                "right": "observations.csv",
                "left_keys": ["animal_id"],
                "right_keys": ["animal_id"],
                "group_by": ["left.genotype"],
                "unit": ["right.animal_id"],
                "unit_metrics": [{"op": "mean", "column": "right.latency_s"}],
                "metrics": [{"op": "count", "name": "n_animals"}],
            },
        ),
        ("list_relationships", {}),
        ("get_evidence", {}),
    ]
    for name, arguments in calls:
        assert _payload(await server.call_tool(name, arguments)), name

    fair = build_server(DatasetService(ingested.output_dir, mode="fair-deterministic"))
    assert _payload(await fair.call_tool("run_fair_check", {}))["results"]
    assert _payload(await fair.call_tool("assess_fair_principles", {}))["results"]
    assert network_guard.attempts == []
