"""Header detection, recording and declared override (D2A-97).

Every fixture is synthetic and built here. The shapes mirror what real lab
files do -- a banner above the header, a key: value preamble before a
behavioural-scoring export, a label row above a repeated metric row -- without
carrying any of their content.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data2agent.cli import main
from data2agent.errors import LayoutError, OutputError
from data2agent.ingest import ingest, layout, tabular
from data2agent.ingest.layout import parse_declarations
from data2agent.mcp import DatasetService

# A behavioural-scoring style export: two "key:" rows that label the columns
# below them, then the real header, then data.
SCORING_TSV = (
    "\tSubjects:\t\tnone\tnone\tnone\tnone\n"
    "\tBehaviors:\t\tgroom\tgroom\tscratch\tscratch\n"
    "Observation\tLength (s)\tInterval\tDuration\tCount\tDuration\tCount\n"
    "obs_1\t600.0\t0-300\t12.5\t3\t4.0\t2\n"
    "obs_1\t600.0\t300-600\t8.0\t1\t0.0\t0\n"
    "obs_2\t598.2\t0-300\t20.1\t5\t1.5\t1\n"
)

# The same kind of export with a sparse key/value preamble, as event logs write it.
PREAMBLE_TSV = (
    "Observation id\trun_7\n"
    "Observation date\t2024-01-01\n"
    "Media FPS\t25\n"
    "\n"
    "Time\tSubject\tBehavior\tStatus\n"
    "1.5\tnone\tgroom\tSTART\n"
    "3.0\tnone\tgroom\tSTOP\n"
)


def _layouts(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "layouts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _dataset(tmp_path: Path, files: dict[str, str]) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    for name, text in files.items():
        (source / name).write_text(text, encoding="utf-8")
    return source


# ------------------------------------------------------------------ detection


def test_a_clean_table_is_unchanged(tmp_path: Path):
    source = _dataset(tmp_path, {"t.csv": "id,group,score\na,x,1\nb,y,2\n"})
    table = ingest(source, tmp_path / "out").manifest["tables"]["t.csv"]

    assert table["header_row"] == 1
    assert table["data_starts_row"] == 2
    assert table["header_source"] == "first-non-empty"
    assert table["header_detection"]["confident"] is True
    assert table["header_detection"]["skipped_rows"] == []
    assert [c["name"] for c in table["columns"]] == ["id", "group", "score"]
    assert table["rows"] == 2


def test_the_example_dataset_keeps_its_first_line_headers(ingested):
    for table in ingested.manifest["tables"].values():
        assert table["header_source"] == "first-non-empty"
        assert table["header_row"] == 1


def test_key_value_label_rows_before_a_tsv_header_are_skipped(tmp_path: Path):
    source = _dataset(tmp_path, {"scores.tsv": SCORING_TSV})
    result = ingest(source, tmp_path / "out")
    table = result.manifest["tables"]["scores.tsv"]

    assert table["header_source"] == "detected"
    assert table["header_row"] == 3
    assert table["data_starts_row"] == 4
    assert table["header_detection"]["skipped_rows"] == [
        {"row": 1, "reason": "key-value-label", "non_empty_cells": 5},
        {"row": 2, "reason": "key-value-label", "non_empty_cells": 5},
    ]
    assert [c["name"] for c in table["columns"]][:3] == ["Observation", "Length (s)", "Interval"]
    assert table["rows"] == 3
    # The decision is a claim of its own, citing the named rule.
    claims = result.evidence.query(subject="scores.tsv", check="table.header-layout")
    assert len(claims) == 1
    evidence = claims[0].evidence[0]
    assert evidence.locator == "line:3"
    assert evidence.result["header_detection"]["rule"] == layout.RULE_ID


def test_a_sparse_key_value_preamble_is_skipped(tmp_path: Path):
    source = _dataset(tmp_path, {"events.tsv": PREAMBLE_TSV})
    table = ingest(source, tmp_path / "out").manifest["tables"]["events.tsv"]

    assert table["header_row"] == 5
    assert [item["row"] for item in table["header_detection"]["skipped_rows"]] == [1, 2, 3]
    assert {item["reason"] for item in table["header_detection"]["skipped_rows"]} == {"sparse"}
    assert [c["name"] for c in table["columns"]] == ["Time", "Subject", "Behavior", "Status"]
    assert table["columns"][0]["dtype"] == "number"
    assert table["rows"] == 2


def test_rows_read_back_after_a_preamble_keep_their_true_line_numbers(tmp_path: Path):
    source = _dataset(tmp_path, {"events.tsv": PREAMBLE_TSV})
    output = tmp_path / "out"
    ingest(source, output)

    payload = DatasetService(output).read_rows("events.tsv", columns=["Time", "Status"])
    assert payload["returned"] == 2
    assert [row["source_row"] for row in payload["rows"]] == [6, 7]
    assert payload["rows"][0]["values"] == {"Time": 1.5, "Status": "START"}

    offset = DatasetService(output).read_rows("events.tsv", columns=["Status"], offset=1)
    assert [row["source_row"] for row in offset["rows"]] == [7]


def test_an_unestablished_header_falls_back_and_says_so(tmp_path: Path):
    # A title line above rows whose first cells are numbers: nothing qualifies as
    # a header, so the first non-empty row is used -- visibly, not silently.
    source = _dataset(tmp_path, {"t.csv": "Report,,\n1,2,3\n4,5,6\n"})
    result = ingest(source, tmp_path / "out")
    table = result.manifest["tables"]["t.csv"]

    assert table["header_source"] == "first-non-empty"
    assert table["header_row"] == 1
    assert table["header_detection"]["confident"] is False
    assert any("looks like a title or preamble" in w for w in result.warnings)


def test_leading_blank_lines_in_a_delimited_file_are_layout(tmp_path: Path):
    source = _dataset(tmp_path, {"t.csv": "\n\nid,score\na,1\n"})
    table = ingest(source, tmp_path / "out").manifest["tables"]["t.csv"]

    assert table["header_row"] == 3
    assert table["header_source"] == "first-non-empty"
    assert [c["name"] for c in table["columns"]] == ["id", "score"]


def test_a_multi_line_quoted_cell_keeps_line_numbers_honest(tmp_path: Path):
    source = _dataset(tmp_path, {"t.csv": 'id,note\na,"two\nlines"\nb,plain\n'})
    output = tmp_path / "out"
    table = ingest(source, output).manifest["tables"]["t.csv"]
    assert table["rows"] == 2

    rows = DatasetService(output).read_rows("t.csv")["rows"]
    # The record ends on line 3; the next one starts and ends on line 4.
    assert [row["source_row"] for row in rows] == [3, 4]


def test_detection_never_composes_a_multi_row_header(tmp_path: Path):
    """A row above the header may be a group label or a title; only a declaration says which."""
    source = _dataset(
        tmp_path, {"t.csv": ",PBS,,DRUG,\nid,Score,Score,Score,Score\na,1,2,3,4\nb,5,6,7,8\n"}
    )
    result = ingest(source, tmp_path / "out")
    table = result.manifest["tables"]["t.csv"]

    assert table["header_row"] == 2
    assert table["header_rows"] == 1
    assert [c["name"] for c in table["columns"]][:2] == ["id", "Score"]
    assert any("declare header_row 1 with header_rows 2" in w for w in result.warnings)


# ------------------------------------------------------------------ workbooks

openpyxl = pytest.importorskip("openpyxl")


def _xlsx(path: Path, rows: list[list[object]], sheet: str = "Sheet1") -> Path:
    book = openpyxl.Workbook()
    book.active.title = sheet
    for row in rows:
        book.active.append(row)
    book.save(path)
    return path


# A registry sheet: a blank row, a banner naming a group of columns, the real
# header, then one row per subject.
REGISTRY = [
    [],
    [None, None, None, None, "Weights", None, None],
    [None, "Cage", "Sex", "ID", "W1", "W2", "W3"],
    [None, "C1", "F", "S01", 20.1, 21.0, 22.3],
    [None, "C1", "M", "S02", 24.0, 25.2, 25.9],
    [None, "C2", "F", "S03", 19.8, 20.4, 21.1],
]


def test_a_banner_row_above_a_sheet_header_is_skipped(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _xlsx(source / "registry.xlsx", REGISTRY)
    output = tmp_path / "out"
    result = ingest(source, output)
    table = result.manifest["tables"]["registry.xlsx#Sheet1"]

    assert table["header_source"] == "detected"
    assert table["header_row"] == 3
    assert table["data_starts_row"] == 4
    assert table["header_detection"]["skipped_rows"] == [
        {"row": 2, "reason": "sparse", "non_empty_cells": 1}
    ]
    names = [c["name"] for c in table["columns"]]
    assert names[1:4] == ["Cage", "Sex", "ID"]
    assert table["rows"] == 3

    rows = DatasetService(output).read_rows("registry.xlsx#Sheet1", columns=["ID", "W1"])
    assert [row["source_row"] for row in rows["rows"]] == [4, 5, 6]
    assert rows["rows"][0]["values"] == {"ID": "S01", "W1": 20.1}


def test_a_header_with_numeric_labels_is_still_detected(tmp_path: Path):
    path = _xlsx(
        tmp_path / "doses.xlsx",
        [
            [20240101, None, "Baseline", None, None],
            ["ID", "Group", "Note", "Sex", 0.02],
            ["S01", "A", 7, "F", 1],
            ["S02", "B", 9, "M", 3],
        ],
    )
    from data2agent.readers import workbook

    sheet = workbook.profile_workbook(path, "doses.xlsx")[0]
    assert sheet.header_row == 2
    assert sheet.layout.source == "detected"
    assert [c.name for c in sheet.columns] == ["ID", "Group", "Note", "Sex", "0.02"]


# ---------------------------------------------------------------- declarations


def test_a_declared_multi_row_header_is_composed_and_recorded(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _xlsx(
        source / "scores.xlsx",
        [
            ["Scores by treatment"],
            [None, "PBS", None, "DRUG", None],
            ["id", "Score", "Score", "Score", "Score"],
            ["a", 1, 2, 3, 4],
            ["b", 5, 6, 7, 8],
        ],
    )
    declarations = _layouts(
        tmp_path,
        {
            "layouts": {
                "scores.xlsx#Sheet1": {
                    "header_row": 2,
                    "header_rows": 2,
                    "upper_label_fill": "forward",
                }
            }
        },
    )
    output = tmp_path / "out"
    result = ingest(source, output, layouts=layout.load_declarations(declarations))
    table = result.manifest["tables"]["scores.xlsx#Sheet1"]

    assert table["header_source"] == "declared"
    assert (table["header_row"], table["header_rows"], table["data_starts_row"]) == (2, 2, 4)
    names = [c["name"] for c in table["columns"]]
    # The upper label is kept, and carried across the group it heads.
    # Duplicate composed names are disambiguated by the sheet profiler's own rule.
    assert names == ["id", "PBS / Score", "PBS / Score.1", "DRUG / Score", "DRUG / Score.1"]
    assert table["columns"][2]["header_cells"] == ["", "Score"]  # as written, before the fill
    assert table["header_detection"]["declared"] == {
        "header_row": 2,
        "header_rows": 2,
        "upper_label_fill": "forward",
    }
    assert table["header_detection"]["skipped_rows"] == [
        {"row": 1, "reason": "declared", "non_empty_cells": 1}
    ]
    rows = DatasetService(output).read_rows("scores.xlsx#Sheet1", columns=["id", "DRUG / Score"])[
        "rows"
    ]
    assert [row["source_row"] for row in rows] == [4, 5]
    assert rows[1]["values"] == {"id": "b", "DRUG / Score": 7}


def test_upper_labels_are_not_carried_across_columns_unless_declared(tmp_path: Path):
    """A banner's span is never guessed: without "forward", each column keeps what
    is written above it, and the fill choice is recorded as declared."""
    source = tmp_path / "source"
    source.mkdir()
    _xlsx(
        source / "scores.xlsx",
        [
            [None, "PBS", None, "DRUG", None],
            ["id", "Score", "Score", "Score", "Score"],
            ["a", 1, 2, 3, 4],
        ],
    )
    declarations = _layouts(
        tmp_path, {"layouts": {"scores.xlsx#Sheet1": {"header_row": 1, "header_rows": 2}}}
    )
    output = tmp_path / "out"
    table = ingest(source, output, layouts=layout.load_declarations(declarations)).manifest[
        "tables"
    ]["scores.xlsx#Sheet1"]

    names = [c["name"] for c in table["columns"]]
    assert names == ["id", "PBS / Score", "Score", "DRUG / Score", "Score.1"]
    assert table["header_detection"]["declared"]["upper_label_fill"] == "none"
    assert table["columns"][2]["header_cells"] == ["", "Score"]


def test_a_declared_header_can_leave_upper_labels_unfilled(tmp_path: Path):
    source = _dataset(tmp_path, {"scores.tsv": SCORING_TSV})
    declarations = parse_declarations(
        {
            "layouts": {
                "scores.tsv": {"header_row": 2, "header_rows": 2, "upper_label_fill": "none"}
            }
        },
        sha256="0" * 64,
        name="layouts.json",
    )
    output = tmp_path / "out"
    table = ingest(source, output, layouts=declarations).manifest["tables"]["scores.tsv"]

    names = [c["name"] for c in table["columns"]]
    assert names[:4] == ["Observation", "Behaviors: / Length (s)", "Interval", "groom / Duration"]
    assert names[5] == "scratch / Duration"
    rows = DatasetService(output).read_rows("scores.tsv", columns=["scratch / Count"])["rows"]
    assert [row["source_row"] for row in rows] == [4, 5, 6]
    assert [row["values"]["scratch / Count"] for row in rows] == [2, 0, 1]


def test_a_declaration_overrides_a_wrong_detection(tmp_path: Path):
    # Detection takes line 1 (two text cells, as wide as the table); the real
    # header is line 2, and the declaration says so.
    text = "---,Channel 1\nTime(s),Signal\n0.0,0.5\n0.1,0.6\n"
    source = _dataset(tmp_path, {"trace.csv": text})
    assert ingest(source, tmp_path / "plain").manifest["tables"]["trace.csv"]["header_row"] == 1

    declarations = parse_declarations(
        {"layouts": {"trace.csv": {"header_row": 2, "note": "line 1 is the device banner"}}},
        sha256="1" * 64,
        name="layouts.json",
    )
    table = ingest(source, tmp_path / "out", layouts=declarations).manifest["tables"]["trace.csv"]
    assert table["header_source"] == "declared"
    assert table["header_row"] == 2
    assert table["header_detection"]["detected_header_row"] == 1  # the override stays visible
    assert table["header_detection"]["declared"]["note"] == "line 1 is the device banner"
    assert [c["name"] for c in table["columns"]] == ["Time(s)", "Signal"]
    assert table["columns"][0]["dtype"] == "number"


def test_a_declared_data_start_skips_rows_below_the_header(tmp_path: Path):
    source = _dataset(tmp_path, {"t.csv": "id,mass\n,(g)\na,1\nb,2\n"})
    declarations = parse_declarations(
        {"layouts": {"t.csv": {"header_row": 1, "data_starts_row": 3}}},
        sha256="2" * 64,
        name="layouts.json",
    )
    output = tmp_path / "out"
    table = ingest(source, output, layouts=declarations).manifest["tables"]["t.csv"]
    assert table["rows"] == 2
    assert table["columns"][1]["dtype"] == "integer"
    assert table["header_detection"]["skipped_rows"] == [
        {"row": 2, "reason": "declared", "non_empty_cells": 1}
    ]
    rows = DatasetService(output).read_rows("t.csv")["rows"]
    assert [row["source_row"] for row in rows] == [3, 4]


def test_a_declaration_naming_an_unknown_table_is_an_error(tmp_path: Path):
    source = _dataset(tmp_path, {"t.csv": "id,score\na,1\n"})
    declarations = parse_declarations(
        {"layouts": {"missing.csv": {"header_row": 1}}}, sha256="3" * 64, name="layouts.json"
    )
    output = tmp_path / "out"
    with pytest.raises(LayoutError, match="missing.csv"):
        ingest(source, output, layouts=declarations)
    assert not (output / "manifest.json").exists()


def test_a_declared_header_beyond_the_table_is_an_error(tmp_path: Path):
    source = _dataset(tmp_path, {"t.csv": "id,score\na,1\n"})
    declarations = parse_declarations(
        {"layouts": {"t.csv": {"header_row": 9}}}, sha256="4" * 64, name="layouts.json"
    )
    with pytest.raises(LayoutError, match="not a row of this table"):
        ingest(source, tmp_path / "out", layouts=declarations)


def test_a_declared_sheet_header_beyond_the_sheet_is_an_error_not_an_unreadable_sheet(
    tmp_path: Path,
):
    source = tmp_path / "source"
    source.mkdir()
    _xlsx(source / "b.xlsx", [["id"], ["a"]])
    declarations = parse_declarations(
        {"layouts": {"b.xlsx#Sheet1": {"header_row": 40}}}, sha256="5" * 64, name="l.json"
    )
    with pytest.raises(LayoutError):
        ingest(source, tmp_path / "out", layouts=declarations)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "JSON object"),
        ({"tables": {}}, "unknown top-level"),
        ({"layouts": {}}, "non-empty 'layouts'"),
        ({"layouts": {"t.csv": {"header_rows": 2}}}, "must state 'header_row'"),
        ({"layouts": {"t.csv": {"header_row": 0}}}, "positive integer"),
        ({"layouts": {"t.csv": {"header_row": True}}}, "positive integer"),
        ({"layouts": {"t.csv": {"header_row": 1, "skip": 2}}}, "unknown key"),
        (
            {"layouts": {"t.csv": {"header_row": 2, "header_rows": 2, "data_starts_row": 3}}},
            "inside",
        ),
        ({"layouts": {"t.csv": {"header_row": 1, "upper_label_fill": "down"}}}, "upper_label_fill"),
        ({"layout_version": "2", "layouts": {"t.csv": {"header_row": 1}}}, "not supported"),
    ],
)
def test_malformed_declarations_are_refused(payload, message):
    with pytest.raises(LayoutError, match=message):
        parse_declarations(payload, sha256="0" * 64, name="layouts.json")


# ------------------------------------------------------- identity and binding


def test_manifests_are_byte_identical_with_and_without_a_declaration(tmp_path: Path):
    source = _dataset(tmp_path, {"scores.tsv": SCORING_TSV, "events.tsv": PREAMBLE_TSV})
    declarations = _layouts(
        tmp_path, {"layouts": {"scores.tsv": {"header_row": 2, "header_rows": 2}}}
    )

    for label, loaded in (("plain", None), ("declared", declarations)):
        first = tmp_path / f"{label}-1"
        second = tmp_path / f"{label}-2"
        for out in (first, second):
            ingest(
                source,
                out,
                layouts=layout.load_declarations(loaded) if loaded else None,
            )
        assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
        assert (first / "evidence.json").read_bytes() == (second / "evidence.json").read_bytes()


def test_the_declaration_digest_is_in_the_manifest_and_the_provenance(tmp_path: Path):
    source = _dataset(tmp_path, {"t.csv": "id,score\na,1\n"})
    path = _layouts(tmp_path, {"layouts": {"t.csv": {"header_row": 1}}})
    result = ingest(source, tmp_path / "out", layouts=layout.load_declarations(path))

    from data2agent.ingest.checksum import hash_file

    digest = hash_file(path)
    assert result.manifest["layout_declaration"] == {"sha256": digest, "tables": ["t.csv"]}
    assert result.provenance["configuration"]["layout_declaration"] == {
        "name": "layouts.json",
        "sha256": digest,
        "tables": ["t.csv"],
    }
    assert ingest(source, tmp_path / "plain").manifest["layout_declaration"] is None


def test_a_relationships_sidecar_is_refused_after_reingest_under_another_layout(
    tmp_path: Path,
):
    source = _dataset(
        tmp_path,
        {
            "subjects.csv": "Registry,\nsubject,group\nS1,a\nS2,b\n",
            "sessions.csv": "subject,session\nS1,1\nS2,1\n",
        },
    )
    output = tmp_path / "out"
    ingest(source, output)
    bundle = DatasetService(output, load_relationships=False).build_relationships(
        [
            {
                "left": "subjects.csv",
                "right": "sessions.csv",
                "left_keys": ["subject"],
                "right_keys": ["subject"],
            }
        ]
    )
    (output / "relationships.json").write_text(json.dumps(bundle), encoding="utf-8")
    assert DatasetService(output).list_relationships(status="declared")["total"] == 1

    declarations = parse_declarations(
        {"layouts": {"subjects.csv": {"header_row": 1}}}, sha256="6" * 64, name="l.json"
    )
    ingest(source, output, layouts=declarations)
    with pytest.raises(OutputError, match="different manifest.json"):
        DatasetService(output)


def test_the_cli_applies_a_layout_and_refuses_an_unknown_table(tmp_path: Path, capsys):
    source = _dataset(tmp_path, {"t.csv": "---,Channel\nTime,Signal\n0,1\n"})
    good = _layouts(tmp_path, {"layouts": {"t.csv": {"header_row": 2}}})
    output = tmp_path / "out"
    assert main(["ingest", str(source), "-o", str(output), "--layout", str(good)]) == 0
    assert "1 declared" in capsys.readouterr().out
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tables"]["t.csv"]["header_row"] == 2

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"layouts": {"nope.csv": {"header_row": 1}}}), encoding="utf-8")
    assert main(["ingest", str(source), "-o", str(tmp_path / "o2"), "--layout", str(bad)]) == 2
    assert "nope.csv" in capsys.readouterr().err


def test_compose_labels_stops_a_fill_at_a_new_upper_group():
    labels, cells = layout.compose_labels(
        [
            ["", "A", "", "", "B", ""],
            ["", "x", "", "y", "", ""],
            ["id", "m", "m", "m", "m", ""],
        ],
        layout.FILL_FORWARD,
    )
    assert labels == ["id", "A / x / m", "A / x / m", "A / y / m", "B / m", ""]
    assert cells[2] == ["", "", "m"]


def test_the_delimited_profile_records_header_fields_even_when_empty(tmp_path: Path):
    path = tmp_path / "empty.csv"
    path.write_text("\n\n", encoding="utf-8")
    profile = tabular.profile_table(path, "empty.csv")
    assert profile is not None
    fields = profile.as_dict()
    assert fields["header_row"] is None
    assert fields["header_source"] is None
    assert fields["rows"] == 0


# ------------------------------------------- duplicate names and applied layouts


def test_repeated_names_in_a_plain_csv_header_stay_addressable(tmp_path: Path):
    """On main the later of two same-named columns shadowed the earlier one."""
    source = _dataset(tmp_path, {"t.csv": "id,score,score,score.1\na,1,2,3\nb,4,5,6\n"})
    output = tmp_path / "out"
    result = ingest(source, output)
    table = result.manifest["tables"]["t.csv"]

    names = [c["name"] for c in table["columns"]]
    # Suffixed against emitted names: the literal 'score.1' is not duplicated.
    assert names == ["id", "score", "score.1", "score.1.1"]
    assert [c["position"] for c in table["columns"]] == [0, 1, 2, 3]
    assert any("repeats 2 column name(s)" in w for w in result.warnings)

    service = DatasetService(output)
    rows = service.read_rows("t.csv")["rows"]
    assert rows[0]["values"] == {"id": "a", "score": 1, "score.1": 2, "score.1.1": 3}
    assert rows[1]["values"] == {"id": "b", "score": 4, "score.1": 5, "score.1.1": 6}

    matched = service.filter_rows(
        "t.csv", filters=[{"column": "score.1", "op": "eq", "value": 5}], columns=["id"]
    )
    assert [row["values"]["id"] for row in matched["rows"]] == ["b"]


def test_a_detected_scoring_header_keeps_every_repeated_metric(tmp_path: Path):
    source = _dataset(tmp_path, {"scores.tsv": SCORING_TSV})
    output = tmp_path / "out"
    table = ingest(source, output).manifest["tables"]["scores.tsv"]
    names = [c["name"] for c in table["columns"]]
    assert names[3:] == ["Duration", "Count", "Duration.1", "Count.1"]

    rows = DatasetService(output).read_rows("scores.tsv", columns=names[3:])["rows"]
    assert rows[0]["values"] == {"Duration": 12.5, "Count": 3, "Duration.1": 4.0, "Count.1": 2}
    assert rows[2]["values"] == {"Duration": 20.1, "Count": 5, "Duration.1": 1.5, "Count.1": 1}


def test_duplicate_composed_labels_in_a_declared_tsv_header_stay_addressable(tmp_path: Path):
    text = "\tA\tA\tB\nid\tscore\tscore\tscore\nr1\t1\t2\t3\nr2\t4\t5\t6\n"
    source = _dataset(tmp_path, {"t.tsv": text})
    declarations = parse_declarations(
        {"layouts": {"t.tsv": {"header_row": 1, "header_rows": 2}}},
        sha256="7" * 64,
        name="l.json",
    )
    output = tmp_path / "out"
    table = ingest(source, output, layouts=declarations).manifest["tables"]["t.tsv"]
    names = [c["name"] for c in table["columns"]]
    assert names == ["id", "A / score", "A / score.1", "B / score"]
    assert table["columns"][2]["header_cells"] == ["A", "score"]

    rows = DatasetService(output).read_rows("t.tsv")["rows"]
    assert rows[0]["values"] == {"id": "r1", "A / score": 1, "A / score.1": 2, "B / score": 3}
    assert rows[1]["values"] == {"id": "r2", "A / score": 4, "A / score.1": 5, "B / score": 6}


def test_a_declaration_for_a_file_that_cannot_be_profiled_is_an_error(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "t.csv").write_bytes(b"id,score\n\xff\xfe,1\n")  # not UTF-8
    declarations = parse_declarations(
        {"layouts": {"t.csv": {"header_row": 1}}}, sha256="8" * 64, name="l.json"
    )
    output = tmp_path / "out"
    with pytest.raises(LayoutError, match="could not apply"):
        ingest(source, output, layouts=declarations)
    assert not (output / "manifest.json").exists()


def test_declarations_carry_no_state_between_ingests(tmp_path: Path):
    declarations = parse_declarations(
        {"layouts": {"t.csv": {"header_row": 1}}}, sha256="9" * 64, name="l.json"
    )
    first = _dataset(tmp_path, {"t.csv": "id,score\na,1\n"})
    ingest(first, tmp_path / "out-1", layouts=declarations)

    # Reusing the same object on a dataset without that table must still fail.
    second = tmp_path / "other"
    second.mkdir()
    (second / "u.csv").write_text("id,score\na,1\n", encoding="utf-8")
    with pytest.raises(LayoutError, match="t.csv"):
        ingest(second, tmp_path / "out-2", layouts=declarations)
