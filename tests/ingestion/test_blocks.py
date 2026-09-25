"""Declared blocks: several tables inside one sheet or delimited file (D2A-103).

Synthetic fixtures only. The shape mirrors a lab results sheet that stacks one
block per session, each under a dated banner and its own header row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data2agent.errors import LayoutError
from data2agent.ingest import ingest
from data2agent.ingest.layout import parse_declarations
from data2agent.mcp import DatasetService

openpyxl = pytest.importorskip("openpyxl")

SHEET = "results"
PARENT = f"sessions.xlsx#{SHEET}"

# Rows 1-5: session 1 (banner, header, three subjects). Row 6 blank.
# Rows 7-10: session 2, with a different header. Rows 11-12: free-text notes.
SESSIONS = [
    ["day 1", None, "baseline"],
    ["ID", "Group", "Score", "Weight"],
    ["S01", "A", 3, 20.5],
    ["S02", "B", 5, 21.0],
    ["S03", "A", 4, 19.5],
    [],
    ["day 2", None, "drug"],
    ["ID", "Drug", "Dose (x5)", "Score"],
    ["S01", "D1", 7, 9],
    ["S02", "D2", 6, 8],
    [],
    ["note: S03 absent on day 2"],
]

BLOCKS = {
    "blocks": [
        {"name": "day1", "header_row": 2, "last_row": 5},
        {"name": "day2", "header_row": 8, "last_row": 10},
    ]
}


def _xlsx(path: Path, rows: list[list[object]], sheet: str = SHEET) -> Path:
    book = openpyxl.Workbook()
    book.active.title = sheet
    for row in rows:
        book.active.append(row)
    book.save(path)
    return path


def _declare(layouts: dict) -> object:
    return parse_declarations({"layouts": layouts}, sha256="0" * 64, name="layouts.json")


def _ingest(tmp_path: Path, layouts: dict, rows=SESSIONS):
    source = tmp_path / "source"
    source.mkdir(parents=True, exist_ok=True)
    _xlsx(source / "sessions.xlsx", rows)
    output = tmp_path / "out"
    return ingest(source, output, layouts=_declare(layouts)), output


def test_stacked_blocks_become_tables_with_their_own_headers(tmp_path: Path):
    result, output = _ingest(tmp_path, {PARENT: BLOCKS})
    tables = result.manifest["tables"]

    # The parent's whole-sheet profile is replaced: keeping it would count every
    # observation twice, under a header that fits only the first block.
    assert PARENT not in tables
    day1, day2 = tables[f"{PARENT}#day1"], tables[f"{PARENT}#day2"]
    assert [c["name"] for c in day1["columns"]] == ["ID", "Group", "Score", "Weight"]
    assert [c["name"] for c in day2["columns"]] == ["ID", "Drug", "Dose (x5)", "Score"]
    assert (day1["rows"], day2["rows"]) == (3, 2)
    assert day2["header_source"] == "declared"
    assert day2["block"] == {
        "name": "day2",
        "parent_table": PARENT,
        "file": "sessions.xlsx",
        "first_row": 6,
        "header_row": 8,
        "last_row": 10,
        "columns": None,
    }
    # Rows between the blocks are the lower block's recorded preamble.
    assert day2["header_detection"]["skipped_rows"] == [
        {"row": 7, "reason": "declared", "non_empty_cells": 2}
    ]

    claims = result.evidence.query(subject=f"{PARENT}#day2", check="layout.block")
    assert len(claims) == 1
    assert claims[0].evidence[0].locator == "rows:6-10"


def test_each_block_reads_back_with_true_source_rows_and_stops_at_last_row(tmp_path: Path):
    _, output = _ingest(tmp_path, {PARENT: BLOCKS})
    service = DatasetService(output)

    day1 = service.read_rows(f"{PARENT}#day1", columns=["ID", "Score"])
    assert [row["source_row"] for row in day1["rows"]] == [3, 4, 5]
    assert day1["rows"][2]["values"] == {"ID": "S03", "Score": 4}

    day2 = service.read_rows(f"{PARENT}#day2", columns=["ID", "Dose (x5)"])
    # Rows 11-12 (a blank and a note) lie below last_row and belong to no block.
    assert [row["source_row"] for row in day2["rows"]] == [9, 10]
    assert day2["rows"][0]["values"] == {"ID": "S01", "Dose (x5)": 7}


def test_aggregate_and_filter_work_on_a_block(tmp_path: Path):
    _, output = _ingest(tmp_path, {PARENT: BLOCKS})
    service = DatasetService(output)

    payload = service.aggregate(
        f"{PARENT}#day1",
        group_by=["Group"],
        metrics=[{"op": "count", "name": "n"}, {"op": "mean", "column": "Score", "name": "m"}],
    )
    groups = {item["group"]["Group"]: item["metrics"] for item in payload["groups"]}
    assert groups["A"] == {"n": 2, "m": pytest.approx(3.5)}
    assert groups["B"]["n"] == 1

    matched = service.filter_rows(
        f"{PARENT}#day2", filters=[{"column": "Drug", "op": "eq", "value": "D2"}]
    )
    assert [row["source_row"] for row in matched["rows"]] == [10]


def test_the_banner_between_blocks_is_readable(tmp_path: Path):
    _, output = _ingest(tmp_path, {PARENT: BLOCKS})
    payload = DatasetService(output).inspect_table(f"{PARENT}#day2", include_rows_above_data=True)
    (banner,) = payload["rows_above_data"]
    assert banner["row"] == 7
    assert [(c["column_letter"], c["value"]) for c in banner["cells"]] == [
        ("A", "day 2"),
        ("C", "drug"),
    ]


def test_the_parent_path_names_its_blocks(tmp_path: Path):
    _, output = _ingest(tmp_path, {PARENT: BLOCKS})
    service = DatasetService(output)
    with pytest.raises(KeyError, match="declared as 2 block"):
        service.inspect_table(PARENT)
    listed = {t["path"]: t for t in service.list_tables()["tables"]}
    assert listed[f"{PARENT}#day1"]["block"]["parent_table"] == PARENT
    assert listed[f"{PARENT}#day1"]["backing_file"] == "sessions.xlsx"


def test_a_block_with_a_multi_row_header(tmp_path: Path):
    rows = [
        [None, "PBS", None, "DRUG", None],
        ["ID", "Score", "Score", "Score", "Score"],
        ["S01", 1, 2, 3, 4],
        ["S02", 5, 6, 7, 8],
    ]
    layouts = {
        PARENT: {
            "blocks": [
                {
                    "name": "scores",
                    "header_row": 1,
                    "header_rows": 2,
                    "last_row": 4,
                    "upper_label_fill": "none",
                }
            ]
        }
    }
    _, output = _ingest(tmp_path, layouts, rows=rows)
    table = DatasetService(output).inspect_table(f"{PARENT}#scores")
    assert [c["name"] for c in table["columns"]] == [
        "ID",
        "PBS / Score",
        "Score",
        "DRUG / Score",
        "Score.1",
    ]
    assert table["data_starts_row"] == 3


def test_side_by_side_blocks_keep_the_sheet_column_positions(tmp_path: Path):
    rows = [
        ["ID", "Score", None, "ID", "Weight"],
        ["S01", 1, None, "S01", 20],
        ["S02", 2, None, "S02", 21],
    ]
    layouts = {
        PARENT: {
            "blocks": [
                {"name": "left", "header_row": 1, "last_row": 3, "columns": "A:B"},
                {"name": "right", "header_row": 1, "last_row": 3, "columns": "D:E"},
            ]
        }
    }
    _, output = _ingest(tmp_path, layouts, rows=rows)
    service = DatasetService(output)
    right = service.inspect_table(f"{PARENT}#right")
    assert [(c["name"], c["position"]) for c in right["columns"]] == [("ID", 3), ("Weight", 4)]
    read = service.read_rows(f"{PARENT}#right")["rows"]
    assert read[1]["values"] == {"ID": "S02", "Weight": 21}


def test_blocks_in_a_delimited_file(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "s.csv").write_text(
        "session 1,,\nid,score,\na,1,\nb,2,\n,,\nsession 2,,\nid,dose,score\na,5,9\n",
        encoding="utf-8",
    )
    layouts = {
        "s.csv": {
            "blocks": [
                {"name": "one", "header_row": 2, "last_row": 4, "columns": "A:B"},
                {"name": "two", "header_row": 7, "last_row": 8},
            ]
        }
    }
    output = tmp_path / "out"
    manifest = ingest(source, output, layouts=_declare(layouts)).manifest
    assert "s.csv" not in manifest["tables"]
    assert manifest["tables"]["s.csv#two"]["block"]["file"] == "s.csv"

    service = DatasetService(output)
    one = service.read_rows("s.csv#one")["rows"]
    assert [(r["source_row"], r["values"]) for r in one] == [
        (3, {"id": "a", "score": 1}),
        (4, {"id": "b", "score": 2}),
    ]
    two = service.read_rows("s.csv#two")["rows"]
    assert [(r["source_row"], r["values"]) for r in two] == [
        (8, {"id": "a", "dose": 5, "score": 9})
    ]
    banner = service.inspect_table("s.csv#two", include_rows_above_data=True)
    assert [r["row"] for r in banner["rows_above_data"]] == [6]


def test_manifests_with_blocks_are_byte_identical_and_match_the_schema(tmp_path: Path):
    jsonschema = pytest.importorskip("jsonschema")
    first, out1 = _ingest(tmp_path / "a", {PARENT: BLOCKS})
    _, out2 = _ingest(tmp_path / "b", {PARENT: BLOCKS})
    assert (out1 / "manifest.json").read_bytes() == (out2 / "manifest.json").read_bytes()
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "dataset-manifest.schema.json"
    jsonschema.validate(first.manifest, json.loads(schema_path.read_text(encoding="utf-8")))


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    ("blocks", "message"),
    [
        (
            [
                {"name": "a", "header_row": 2, "last_row": 6},
                {"name": "b", "header_row": 6, "last_row": 9},
            ],
            "overlap",
        ),
        (
            [
                {"name": "a", "header_row": 1, "last_row": 3},
                {"name": "a", "header_row": 5, "last_row": 7},
            ],
            "unique",
        ),
        ([{"name": "a#b", "header_row": 1, "last_row": 3}], "name"),
        ([{"name": "a", "header_row": 1}], "last_row"),
        ([{"name": "a", "header_row": 4, "last_row": 3}], "ends before"),
        ([{"name": "a", "header_row": 1, "last_row": 3, "columns": "K:A"}], "backwards"),
        ([{"name": "a", "header_row": 1, "last_row": 3, "columns": "A-K"}], "column letters"),
        ([{"name": "a", "header_row": 1, "last_row": 3, "skip": 1}], "unknown key"),
        ([], "non-empty"),
    ],
)
def test_malformed_blocks_are_refused(blocks, message):
    with pytest.raises(LayoutError, match=message):
        _declare({PARENT: {"blocks": blocks}})


def test_blocks_cannot_be_mixed_with_a_table_header():
    with pytest.raises(LayoutError, match="declares blocks"):
        _declare(
            {PARENT: {"header_row": 1, "blocks": [{"name": "a", "header_row": 1, "last_row": 2}]}}
        )


def test_side_by_side_blocks_in_disjoint_columns_do_not_overlap():
    _declare(
        {
            PARENT: {
                "blocks": [
                    {"name": "l", "header_row": 1, "last_row": 5, "columns": "A:C"},
                    {"name": "r", "header_row": 1, "last_row": 5, "columns": "D:F"},
                ]
            }
        }
    )


def test_a_block_beyond_the_sheet_is_an_error(tmp_path: Path):
    with pytest.raises(LayoutError, match="last row is 12"):
        _ingest(tmp_path, {PARENT: {"blocks": [{"name": "a", "header_row": 2, "last_row": 40}]}})


def test_a_block_header_outside_the_sheet_is_an_error(tmp_path: Path):
    with pytest.raises(LayoutError, match="not a row of this table"):
        _ingest(tmp_path, {PARENT: {"blocks": [{"name": "a", "header_row": 50, "last_row": 60}]}})


def test_blocks_for_a_sheet_that_does_not_exist_are_an_error(tmp_path: Path):
    with pytest.raises(LayoutError, match="could not apply"):
        _ingest(tmp_path, {"sessions.xlsx#missing": BLOCKS})
