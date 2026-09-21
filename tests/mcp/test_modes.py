"""Benchmark modes: implemented ones work, unimplemented ones fail loudly."""

from __future__ import annotations

import pytest

from data2agent.errors import ModeError
from data2agent.mcp import MODES, DatasetService, resolve_mode


def test_raw_mode_exposes_only_filesystem_like_tools(ingested):
    service = DatasetService(ingested.output_dir, mode="raw")
    assert service.available_tools() == ["list_files", "inspect_file"]
    assert not service.supports("get_evidence")
    assert not service.supports("inspect_table")


def test_structured_mode_exposes_the_full_v01_surface(ingested):
    service = DatasetService(ingested.output_dir, mode="structured")
    for tool in (
        "dataset_inventory",
        "list_files",
        "inspect_file",
        "inspect_table",
        "get_metadata",
        "get_evidence",
    ):
        assert service.supports(tool)


@pytest.mark.parametrize("mode", ["fair-skill", "fair-semantic"])
def test_unimplemented_modes_refuse_rather_than_downgrade(mode, ingested):
    """A silent downgrade would corrupt a benchmark run without any signal."""
    with pytest.raises(ModeError, match="not implemented"):
        DatasetService(ingested.output_dir, mode=mode)


def test_unknown_mode_is_rejected():
    with pytest.raises(ModeError, match="unknown mode"):
        resolve_mode("fair-vibes")


@pytest.mark.parametrize("mode", ["fair-rules", "fair-deterministic"])
def test_the_fair_modes_are_available(mode, ingested):
    service = DatasetService(ingested.output_dir, mode=mode)
    assert service.supports("get_fair_indicator")
    assert service.supports("list_fair_rules")
    # Every FAIR mode is a superset of structured: the ladder varies the form of
    # the constraint, never the information underneath it.
    assert service.supports("inspect_table") and service.supports("get_evidence")


def test_fair_rules_withholds_the_deterministic_checks(ingested):
    """The point of the mode: the agent reads the rules and must run them itself."""
    service = DatasetService(ingested.output_dir, mode="fair-rules")
    assert not service.supports("run_fair_check")
    assert DatasetService(ingested.output_dir, mode="fair-deterministic").supports("run_fair_check")


def test_structured_mode_exposes_no_fair_concept(ingested):
    """If FAIR leaks into the control condition, the control stops being one."""
    import json

    service = DatasetService(ingested.output_dir, mode="structured")
    assert not any("fair" in tool for tool in service.available_tools())
    surface = json.dumps(
        [
            service.dataset_inventory(),
            service.get_provenance(),
            service.inspect_table("animals.csv"),
            service.get_metadata(),
        ]
    ).lower()
    for term in ("fair", "findable", "interoperable", "reusable", "f1-", "r1."):
        assert term not in surface, f"the structured surface mentions '{term}'"


def test_every_declared_mode_lists_its_tools():
    assert MODES, "the mode registry must not be empty"
    for mode in MODES.values():
        assert mode.tools
        assert mode.description
