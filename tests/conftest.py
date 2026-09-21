"""Shared fixtures.

The example dataset is treated as a read-only fixture: tests that need to mutate
a dataset copy it into ``tmp_path`` first. A test that modified
``examples/preclinical-minimal`` would change its ``dataset_id`` and silently
invalidate every other test's expected numbers.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from data2agent.ingest import ingest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DATASET = REPO_ROOT / "examples" / "preclinical-minimal"


@pytest.fixture(scope="session")
def example_dataset() -> Path:
    assert EXAMPLE_DATASET.is_dir(), f"example dataset missing at {EXAMPLE_DATASET}"
    return EXAMPLE_DATASET


@pytest.fixture
def dataset_copy(example_dataset: Path, tmp_path: Path) -> Path:
    """A writable copy of the example dataset, for tests that need to disturb it."""
    destination = tmp_path / "dataset"
    shutil.copytree(example_dataset, destination)
    return destination


@pytest.fixture
def ingested(example_dataset: Path, tmp_path: Path):
    return ingest(example_dataset, tmp_path / "out")


@pytest.fixture
def anyio_backend() -> str:
    """The MCP binding tests are async; asyncio is the only backend we need."""
    return "asyncio"
