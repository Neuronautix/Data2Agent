"""Acceptance: repeated ingest of the same bytes gives the same manifest."""

from __future__ import annotations

import json
from pathlib import Path

from data2agent.ingest import ingest
from data2agent.ingest.checksum import dataset_id


def test_repeated_ingest_produces_byte_identical_manifest(example_dataset: Path, tmp_path: Path):
    first = ingest(example_dataset, tmp_path / "a")
    second = ingest(example_dataset, tmp_path / "b")

    assert first.dataset_id == second.dataset_id
    assert first.manifest == second.manifest
    assert (tmp_path / "a" / "manifest.json").read_bytes() == (
        tmp_path / "b" / "manifest.json"
    ).read_bytes()


def test_repeated_ingest_produces_identical_evidence(example_dataset: Path, tmp_path: Path):
    first = ingest(example_dataset, tmp_path / "a")
    second = ingest(example_dataset, tmp_path / "b")
    assert first.evidence.as_dict() == second.evidence.as_dict()


def test_manifest_carries_no_run_specific_state(ingested, example_dataset: Path):
    """A manifest containing a clock reading or a machine path cannot be compared."""
    serialised = json.dumps(ingested.manifest)
    assert str(example_dataset) not in serialised, "manifest must not embed absolute paths"
    for forbidden in ("ingested_at", "started_at", "finished_at", "executable", "platform"):
        assert forbidden not in ingested.manifest

    # ...and provenance is where those facts actually live.
    assert ingested.provenance["started_at"]
    assert ingested.provenance["finished_at"]
    assert ingested.provenance["source_path"] == str(example_dataset)


def test_dataset_id_changes_when_any_byte_changes(dataset_copy: Path, tmp_path: Path):
    before = ingest(dataset_copy, tmp_path / "before").dataset_id
    target = dataset_copy / "animals.csv"
    target.write_text(target.read_text().replace("C57BL/6J", "C57BL/6N", 1), encoding="utf-8")
    after = ingest(dataset_copy, tmp_path / "after").dataset_id
    assert before != after


def test_dataset_id_changes_when_a_file_is_renamed(dataset_copy: Path, tmp_path: Path):
    before = ingest(dataset_copy, tmp_path / "before").dataset_id
    (dataset_copy / "animals.csv").rename(dataset_copy / "subjects.csv")
    after = ingest(dataset_copy, tmp_path / "after").dataset_id
    assert before != after


def test_dataset_id_is_independent_of_walk_order():
    pairs = [("b.csv", "a" * 64), ("a.csv", "b" * 64)]
    assert dataset_id(pairs) == dataset_id(reversed(pairs))
