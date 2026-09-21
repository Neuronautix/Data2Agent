"""Acceptance: the source bytes are unchanged by ingestion."""

from __future__ import annotations

from pathlib import Path

import pytest

from data2agent.errors import OutputError
from data2agent.ingest import ingest
from data2agent.ingest.checksum import hash_file


def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.relative_to(root).as_posix(): (hash_file(path), path.stat().st_size)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_ingest_does_not_touch_the_source(dataset_copy: Path, tmp_path: Path):
    before = _snapshot(dataset_copy)
    ingest(dataset_copy, tmp_path / "out")
    assert _snapshot(dataset_copy) == before


def test_ingest_writes_nothing_into_the_source(dataset_copy: Path, tmp_path: Path):
    names_before = {path.name for path in dataset_copy.rglob("*")}
    ingest(dataset_copy, tmp_path / "out")
    assert {path.name for path in dataset_copy.rglob("*")} == names_before


def test_every_file_is_checksummed(ingested):
    entries = ingested.manifest["files"]
    assert entries
    assert all(len(entry["sha256"]) == 64 for entry in entries)
    assert ingested.manifest["file_count"] == len(entries)


def test_output_inside_the_source_is_refused(dataset_copy: Path):
    """Writing the manifest into the dataset would change the thing being described."""
    with pytest.raises(OutputError):
        ingest(dataset_copy, dataset_copy / "agent-output")


def test_provenance_records_that_the_source_was_verified(ingested):
    assert ingested.provenance["source_verified_unchanged"] is True
    assert ingested.provenance["source_mutated"] is False
