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


@pytest.mark.parametrize(
    "mode", ["fair-skill", "fair-rules", "fair-deterministic", "fair-semantic"]
)
def test_unimplemented_modes_refuse_rather_than_downgrade(mode, ingested):
    """A silent downgrade would corrupt a benchmark run without any signal."""
    with pytest.raises(ModeError, match="not implemented"):
        DatasetService(ingested.output_dir, mode=mode)


def test_unknown_mode_is_rejected():
    with pytest.raises(ModeError, match="unknown mode"):
        resolve_mode("fair-vibes")


def test_every_declared_mode_lists_its_tools():
    assert MODES, "the mode registry must not be empty"
    for mode in MODES.values():
        assert mode.tools
        assert mode.description
