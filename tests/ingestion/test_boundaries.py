"""The dataset boundary, and the byte-level surprises real datasets carry.

Regression tests for review findings on PR #1. Each fails loudly if the old
behaviour returns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data2agent.ingest import ingest
from data2agent.ingest.inventory import build
from data2agent.ingest.pipeline import _verify_source_unchanged
from data2agent.ingest.textio import decode


def test_a_symlink_to_a_file_is_never_followed(tmp_path: Path):
    """`is_file()` follows symlinks, so a link to a regular file used to be
    hashed and profiled -- putting bytes from outside the dataset into a
    manifest that claims to describe it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host data that is not part of the dataset\n")

    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "real.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (dataset / "link.txt").symlink_to(outside / "secret.txt")

    result = ingest(dataset, tmp_path / "out")

    assert [entry["path"] for entry in result.manifest["files"]] == ["real.csv"]
    assert "link.txt (symlink not followed)" in result.manifest["skipped"]
    # Silence would read as "there were no symlinks", so the omission is loud.
    assert any("symlink" in warning for warning in result.manifest["warnings"])


def test_a_symlink_to_a_directory_is_never_followed(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_text("a\n1\n", encoding="utf-8")

    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "real.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (dataset / "sub").symlink_to(outside, target_is_directory=True)

    result = ingest(dataset, tmp_path / "out")
    assert [entry["path"] for entry in result.manifest["files"]] == ["real.csv"]


def test_a_bom_does_not_end_up_in_a_column_name(tmp_path: Path):
    """utf-8 decodes BOM-bearing bytes successfully, so a utf-8-sig *fallback*
    never runs and \\ufeff propagates into every claim about the first column.
    Spreadsheet exports carry a BOM routinely."""
    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "bom.csv").write_bytes("animal_id,sex\nA1,M\n".encode("utf-8-sig"))

    result = ingest(dataset, tmp_path / "out")
    profile = result.manifest["tables"]["bom.csv"]

    assert [column["name"] for column in profile["columns"]] == ["animal_id", "sex"]
    assert profile["encoding"] == "utf-8-sig"
    assert any("byte-order mark" in warning for warning in result.manifest["warnings"])

    for claim in result.evidence.claims:
        assert "﻿" not in claim.claim


def test_a_bom_bearing_json_still_parses(tmp_path: Path):
    dataset = tmp_path / "ds"
    dataset.mkdir()
    (dataset / "metadata.json").write_bytes('{"Name": "x"}'.encode("utf-8-sig"))

    result = ingest(dataset, tmp_path / "out")
    profile = result.manifest["structured"]["metadata.json"]
    assert profile["root_type"] == "object"
    assert "parse_error" not in profile


def test_decode_reports_the_encoding_it_actually_used():
    assert decode(b"a,b\n") == ("a,b\n", "utf-8")
    assert decode("a,b\n".encode("utf-8-sig")) == ("a,b\n", "utf-8-sig")
    assert decode(b"\xff\xfe\x00\x01") == (None, "")


def test_a_file_appearing_mid_run_breaks_the_unchanged_claim(dataset_copy: Path):
    """Re-hashing only the known entries would pass while the manifest silently
    described less than the directory contains."""
    from data2agent.ingest.inventory import DEFAULT_EXCLUDES

    inventory = build(dataset_copy)
    assert _verify_source_unchanged(dataset_copy, inventory, DEFAULT_EXCLUDES) is None

    (dataset_copy / "late.csv").write_text("a\n1\n", encoding="utf-8")
    drift = _verify_source_unchanged(dataset_copy, inventory, DEFAULT_EXCLUDES)
    assert drift is not None and "late.csv" in drift


def test_a_file_disappearing_mid_run_breaks_the_unchanged_claim(dataset_copy: Path):
    from data2agent.ingest.inventory import DEFAULT_EXCLUDES

    inventory = build(dataset_copy)
    (dataset_copy / "animals.csv").unlink()
    drift = _verify_source_unchanged(dataset_copy, inventory, DEFAULT_EXCLUDES)
    assert drift is not None and "animals.csv" in drift


def test_changed_content_mid_run_breaks_the_unchanged_claim(dataset_copy: Path):
    from data2agent.ingest.inventory import DEFAULT_EXCLUDES

    inventory = build(dataset_copy)
    (dataset_copy / "animals.csv").write_text("animal_id\nA001\n", encoding="utf-8")
    drift = _verify_source_unchanged(dataset_copy, inventory, DEFAULT_EXCLUDES)
    assert drift is not None and "checksum" in drift


@pytest.mark.parametrize("name", ["real.csv", "nested/deep.csv"])
def test_ordinary_files_are_unaffected_by_the_symlink_guard(tmp_path: Path, name: str):
    dataset = tmp_path / "ds"
    (dataset / "nested").mkdir(parents=True)
    (dataset / name).write_text("a,b\n1,2\n", encoding="utf-8")
    result = ingest(dataset, tmp_path / "out")
    assert name in {entry["path"] for entry in result.manifest["files"]}
