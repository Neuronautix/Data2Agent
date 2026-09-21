"""Inventory, format detection, metadata recognition and identifier extraction."""

from __future__ import annotations

from pathlib import Path

from data2agent.ingest import formats, identifiers, metadata
from data2agent.ingest.inventory import build


def test_inventory_is_sorted_and_relative(example_dataset: Path):
    inventory = build(example_dataset)
    paths = [entry.path for entry in inventory.files]
    assert paths == sorted(paths)
    assert paths == ["README.md", "animals.csv", "dataset_description.json", "observations.csv"]
    assert all(not path.startswith("/") for path in paths)


def test_unknown_formats_stay_unknown(tmp_path: Path):
    path = tmp_path / "instrument.xyzzy"
    path.write_bytes(b"\x00\x01\x02\x03")
    detected, notes = formats.detect(path)
    assert detected.format_id == "unknown"
    assert detected.detected_by == "none"
    assert notes, "an unrecognised format must produce a warning, not silence"


def test_bytes_outrank_a_misleading_extension(tmp_path: Path):
    path = tmp_path / "table.csv"
    path.write_bytes(b"\x89PNG\r\n\x1a\nnot really a csv")
    detected, notes = formats.detect(path)
    assert detected.format_id == "png"
    assert detected.detected_by == "signature"
    assert any("leading bytes" in note for note in notes)


def test_metadata_files_are_recognised_by_convention():
    assert metadata.classify("dataset_description.json").convention == "bids"
    assert metadata.classify("ro-crate-metadata.json").convention == "ro-crate"
    assert metadata.classify("README.md").convention == "readme"
    assert metadata.classify("animals.csv") is None


def test_identifiers_are_detected_with_their_location(example_dataset: Path):
    hits = identifiers.scan_text_file(example_dataset / "README.md", "README.md")
    by_scheme = {hit.scheme: hit for hit in hits}
    assert by_scheme["doi"].value == "10.5281/zenodo.0000000"
    assert by_scheme["orcid"].value == "0000-0002-1825-0097"
    assert by_scheme["rrid"].value == "RRID:IMSR_JAX:000664"
    assert all(hit.line > 0 and hit.source == "README.md" for hit in hits)


def test_excluded_directories_are_reported_not_silently_dropped(dataset_copy: Path):
    (dataset_copy / ".git").mkdir()
    (dataset_copy / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    inventory = build(dataset_copy)
    assert ".git" in inventory.skipped
    assert all(not entry.path.startswith(".git") for entry in inventory.files)
