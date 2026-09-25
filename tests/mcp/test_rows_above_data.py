"""Rows above a table's data stay readable after the header rule skips them (D2A-102).

Synthetic fixtures only. The shapes mirror real lab files: a date/label banner
above a sheet's real header, and a behavioural-scoring export whose "key:" rows
label the columns below them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data2agent.ingest import ingest
from data2agent.ingest.layout import parse_declarations
from data2agent.mcp import DatasetService

SCORING_TSV = (
    "\tSubjects:\t\tnone\tnone\tnone\tnone\n"
    "\tBehaviors:\t\tgroom\tgroom\tscratch\tNA\n"
    "Observation\tLength (s)\tInterval\tDuration\tCount\tDuration\tCount\n"
    "obs_1\t600.0\t0-300\t12.5\t3\t4.0\t2\n"
)


def _dataset(tmp_path: Path, files: dict[str, str]) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    for name, text in files.items():
        (source / name).write_text(text, encoding="utf-8")
    return source


def test_a_clean_table_has_no_rows_above_its_data(tmp_path: Path):
    source = _dataset(tmp_path, {"t.csv": "id,score\na,1\n"})
    ingest(source, tmp_path / "out")
    service = DatasetService(tmp_path / "out")

    assert service.inspect_table("t.csv")["rows_above_data_available"] == 0
    payload = service.inspect_table("t.csv", include_rows_above_data=True)
    assert payload["rows_above_data"] == []
    assert payload["rows_truncated"] is False


def test_the_default_inspection_counts_without_reading_cells(tmp_path: Path):
    source = _dataset(tmp_path, {"scores.tsv": SCORING_TSV})
    ingest(source, tmp_path / "out")
    payload = DatasetService(tmp_path / "out").inspect_table("scores.tsv")

    assert payload["rows_above_data_available"] == 2
    assert "rows_above_data" not in payload


def test_a_key_value_preamble_is_readable_with_its_line_numbers(tmp_path: Path):
    source = _dataset(tmp_path, {"scores.tsv": SCORING_TSV})
    ingest(source, tmp_path / "out")
    payload = DatasetService(tmp_path / "out").inspect_table(
        "scores.tsv", include_rows_above_data=True
    )

    assert payload["row_locator"] == "1-based line a record starts on"
    first, second = payload["rows_above_data"]
    assert (first["row"], first["role"], first["reason"]) == (1, "skipped", "key-value-label")
    assert first["non_empty_cells"] == 5
    assert first["cells"][0] == {
        "position": 1,
        "column_letter": "B",
        "table_column": "Length (s)",
        "value": "Subjects:",
    }
    behaviours = {cell["table_column"]: cell["value"] for cell in second["cells"]}
    assert behaviours["Duration"] == "groom"
    assert behaviours["Duration.1"] == "scratch"
    # Normalised as read_rows normalises a cell: a resolved token is null, kept raw.
    sentinel = second["cells"][-1]
    assert sentinel["value"] is None
    assert sentinel["missing"] == {"kind": "sentinel", "raw": "NA"}


def test_rows_and_cells_are_bounded_and_the_truncation_is_reported(tmp_path: Path):
    source = _dataset(tmp_path, {"scores.tsv": SCORING_TSV})
    ingest(source, tmp_path / "out")
    service = DatasetService(tmp_path / "out")
    payload = service.inspect_table(
        "scores.tsv", include_rows_above_data=True, max_rows=1, max_cells=2
    )

    assert payload["rows_available"] == 2
    assert payload["rows_truncated"] is True
    (row,) = payload["rows_above_data"]
    assert row["row"] == 1
    assert len(row["cells"]) == 2
    assert row["non_empty_cells"] == 5
    assert row["cells_truncated"] is True
    assert payload["rows_above_data_bounds"] == {"max_rows": 1, "max_cells": 2}

    with pytest.raises(ValueError, match="max_rows"):
        service.inspect_table("scores.tsv", include_rows_above_data=True, max_rows=0)


def test_the_cells_are_withheld_after_the_backing_file_changes(tmp_path: Path):
    source = _dataset(tmp_path, {"scores.tsv": SCORING_TSV})
    ingest(source, tmp_path / "out")
    (source / "scores.tsv").write_text(SCORING_TSV + "obs_2\t1\t1\t1\t1\t1\t1\n", encoding="utf-8")

    payload = DatasetService(tmp_path / "out").inspect_table(
        "scores.tsv", include_rows_above_data=True
    )
    assert payload["integrity"]["matches"] is False
    assert payload["rows_above_data"] == []
    assert "re-ingest" in payload["content_withheld"]


def test_a_declared_multi_row_header_exposes_its_raw_header_rows(tmp_path: Path):
    source = _dataset(tmp_path, {"t.csv": ",A,,B\nid,score,score,score\nr1,1,2,3\n"})
    declarations = parse_declarations(
        {"layouts": {"t.csv": {"header_row": 1, "header_rows": 2}}},
        sha256="0" * 64,
        name="l.json",
    )
    ingest(source, tmp_path / "out", layouts=declarations)
    payload = DatasetService(tmp_path / "out").inspect_table("t.csv", include_rows_above_data=True)

    assert [(r["row"], r["role"]) for r in payload["rows_above_data"]] == [
        (1, "header"),
        (2, "header"),
    ]
    assert [c["value"] for c in payload["rows_above_data"][0]["cells"]] == ["A", "B"]


def test_header_rows_after_a_multiline_record_keep_their_real_line_numbers(tmp_path: Path):
    """The first header record spans lines 1-2, so the second one starts on line 3."""
    source = _dataset(tmp_path, {"t.csv": ',"A\ncontinued",,B\nid,score,score,score\nr1,1,2,3\n'})
    declarations = parse_declarations(
        {"layouts": {"t.csv": {"header_row": 1, "header_rows": 2}}},
        sha256="0" * 64,
        name="l.json",
    )
    ingest(source, tmp_path / "out", layouts=declarations)
    payload = DatasetService(tmp_path / "out").inspect_table("t.csv", include_rows_above_data=True)

    rows = payload["rows_above_data"]
    assert [(r["row"], r["role"]) for r in rows] == [(1, "header"), (3, "header")]
    # The quoted newline is whatever the platform wrote; only its presence matters.
    assert [c["value"].splitlines() for c in rows[0]["cells"]] == [["A", "continued"], ["B"]]
    assert [c["value"] for c in rows[1]["cells"]] == ["id", "score", "score", "score"]
    assert all("header_offset" not in r for r in rows)


openpyxl = pytest.importorskip("openpyxl")


def test_a_sheet_banner_is_readable_with_its_row_number_and_iso_dates(tmp_path: Path):
    from datetime import datetime

    source = tmp_path / "source"
    source.mkdir()
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "results"
    sheet.append([datetime(2024, 3, 1), None, "Baseline"])
    sheet.append(["ID", "Group", "Note", "Sex", "Score"])
    sheet.append(["S01", "A", 7, "F", 1])
    sheet.append(["S02", "B", 9, "M", 3])
    book.save(source / "results.xlsx")

    output = tmp_path / "out"
    manifest = ingest(source, output).manifest
    table = manifest["tables"]["results.xlsx#results"]
    assert table["header_row"] == 2
    # Counts only in the manifest; the banner's values are read at query time.
    assert "2024" not in json.dumps(table)

    payload = DatasetService(output).inspect_table(
        "results.xlsx#results", include_rows_above_data=True
    )
    assert payload["row_locator"] == "1-based worksheet row"
    (banner,) = payload["rows_above_data"]
    assert (banner["row"], banner["role"], banner["reason"]) == (1, "skipped", "sparse")
    assert banner["cells"] == [
        {"position": 0, "column_letter": "A", "table_column": "ID", "value": "2024-03-01T00:00:00"},
        {"position": 2, "column_letter": "C", "table_column": "Note", "value": "Baseline"},
    ]
