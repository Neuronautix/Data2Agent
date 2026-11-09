"""Semantic tools are opt-in: absent by default, present only with a pinned snapshot and policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from data2agent.cli import main
from data2agent.errors import ModeError, OutputError, QueryValidationError
from data2agent.mcp import MODES, DatasetService
from data2agent.mcp.service import SEMANTIC_RETRIEVAL_TOOLS, SEMANTIC_TOOLS

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "semantic"


def _service(ingested, mode="structured", tools=None):
    return DatasetService(
        ingested.output_dir,
        mode=mode,
        semantic_snapshot=EXAMPLE / "snapshot.json",
        semantic_policy=EXAMPLE / "policy.json",
        semantic_tools=tools,
    )


@pytest.mark.parametrize("mode", ["raw", "structured", "fair-rules", "fair-deterministic"])
def test_default_tool_lists_are_unchanged(ingested, mode):
    service = DatasetService(ingested.output_dir, mode=mode)
    assert service.available_tools() == list(MODES[mode].tools)
    assert not set(SEMANTIC_TOOLS) & set(service.available_tools())


def test_fair_semantic_mode_stays_unavailable(ingested):
    with pytest.raises(ModeError, match="not implemented"):
        DatasetService(ingested.output_dir, mode="fair-semantic")


def test_attached_tools_answer(ingested):
    service = _service(ingested)
    assert service.available_tools()[-3:] == list(SEMANTIC_RETRIEVAL_TOOLS)
    found = service.search_ontology_terms("mouse", "species", 3)
    assert found["candidates"][0]["curie"] == "NCBITaxon:10090"
    with pytest.raises(QueryValidationError, match="not exposed"):
        service.validate_semantic_mapping([])
    service = _service(ingested, tools=list(SEMANTIC_TOOLS))
    verdict = service.validate_semantic_mapping(
        [{"field": "species", "decision": "map", "value": "NCBITaxon:9606"}]
    )
    assert verdict["formally_valid"] is False
    with pytest.raises(QueryValidationError):
        service.search_ontology_terms("mouse", "no_such_field", 3)


def test_snapshot_and_policy_go_together(ingested):
    with pytest.raises(OutputError):
        DatasetService(ingested.output_dir, semantic_snapshot=EXAMPLE / "snapshot.json")


def test_record_tool_appears_only_for_a_bound_record(ingested):
    assert "get_semantic_mapping" not in _service(ingested).available_tools()
    assert (
        main(
            [
                "semantic",
                str(ingested.output_dir),
                "--snapshot",
                str(EXAMPLE / "snapshot.json"),
                "--policy",
                str(EXAMPLE / "policy.json"),
                "--bindings",
                str(EXAMPLE / "bindings.preclinical-minimal.json"),
            ]
        )
        == 0
    )
    assert "get_semantic_mapping" not in _service(ingested).available_tools()  # never by default
    service = _service(ingested, tools=["get_semantic_mapping"])
    assert service.available_tools()[-1:] == ["get_semantic_mapping"]
    assert service.get_semantic_mapping()["dataset_id"] == service.dataset_id


@pytest.mark.anyio
async def test_tools_are_registered_over_mcp_only_when_attached(ingested):
    pytest.importorskip("mcp")
    from data2agent.mcp.server import build_server

    plain = {t.name for t in await build_server(DatasetService(ingested.output_dir)).list_tools()}
    attached = {t.name for t in await build_server(_service(ingested)).list_tools()}
    assert not set(SEMANTIC_TOOLS) & plain
    assert attached - plain == set(SEMANTIC_RETRIEVAL_TOOLS)
