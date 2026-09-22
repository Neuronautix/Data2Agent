"""OOXML workbook profiling (D2A-47)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from data2agent.ingest import formats
from data2agent.ingest.pipeline import ingest

openpyxl = pytest.importorskip("openpyxl")

from data2agent.readers import workbook  # noqa: E402


def _write(path: Path, rows: list[list[object]], sheet: str = "Sheet1", *, lead_blank: int = 0):
    book = openpyxl.Workbook()
    ws = book.active
    ws.title = sheet
    for _ in range(lead_blank):
        ws.append([])
    for row in rows:
        ws.append(row)
    book.save(path)
    return path


def test_a_workbook_yields_one_table_per_sheet(tmp_path: Path):
    path = tmp_path / "book.xlsx"
    book = openpyxl.Workbook()
    book.active.title = "first"
    book.active.append(["a", "b"])
    book.active.append([1, 2])
    second = book.create_sheet("second")
    second.append(["x"])
    second.append([9])
    book.save(path)

    sheets = workbook.profile_workbook(path, "book.xlsx")
    assert [s.sheet for s in sheets] == ["first", "second"]
    # The key carries the sheet, so two sheets cannot collide in the manifest.
    assert [s.path for s in sheets] == ["book.xlsx#first", "book.xlsx#second"]
    assert [s.rows for s in sheets] == [1, 1]


def test_missingness_uses_the_same_convention_as_csv(tmp_path: Path):
    """A token must mean the same thing in a sheet as in a delimited file."""
    path = _write(tmp_path / "m.xlsx", [["id", "sex"], [1, "M"], [2, None], [3, "NA"]])

    sheet = workbook.profile_workbook(path, "m.xlsx")[0]
    sex = next(c for c in sheet.columns if c.name == "sex")

    assert sheet.rows == 3
    assert sex.missing_empty == 1  # the blank cell
    assert sex.missing_sentinel == 1  # 'NA', under default-sentinels
    assert sex.missing == 2
    # The invariant the delimited profiler guarantees holds here too.
    assert sex.missing == sex.missing_empty + sex.missing_sentinel
    assert sex.values + sex.missing == sheet.rows


def test_the_header_row_is_recorded_not_assumed(tmp_path: Path):
    """XP14's own registry starts on row 2; the row used must be reportable."""
    path = _write(tmp_path / "late.xlsx", [["id", "sex"], [1, "F"]], lead_blank=3)

    sheet = workbook.profile_workbook(path, "late.xlsx")[0]
    assert sheet.header_row == 4
    assert sheet.has_header is True
    assert [c.name for c in sheet.columns] == ["id", "sex"]
    assert sheet.rows == 1


def test_blank_header_cells_are_named_not_dropped(tmp_path: Path):
    path = _write(tmp_path / "gappy.xlsx", [["id", None, "sex"], [1, "x", "F"]])

    sheet = workbook.profile_workbook(path, "gappy.xlsx")[0]
    # Dropping the unnamed column would under-report the sheet's width.
    assert [c.name for c in sheet.columns] == ["id", "B", "sex"]
    assert any("blank cell" in w for w in sheet.warnings)


def test_duplicate_header_names_are_disambiguated(tmp_path: Path):
    path = _write(tmp_path / "dup.xlsx", [["id", "id"], [1, 2]])

    sheet = workbook.profile_workbook(path, "dup.xlsx")[0]
    assert [c.name for c in sheet.columns] == ["id", "id.1"]


def test_an_empty_sheet_is_reported_not_silently_skipped(tmp_path: Path):
    path = tmp_path / "empty.xlsx"
    openpyxl.Workbook().save(path)

    sheet = workbook.profile_workbook(path, "empty.xlsx")[0]
    assert sheet.rows == 0
    assert sheet.has_header is False
    assert any("empty" in w for w in sheet.warnings)


def test_a_corrupt_workbook_is_reported_not_raised(tmp_path: Path):
    broken = tmp_path / "broken.xlsx"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "not xml at all <<<")

    sheets = workbook.profile_workbook(broken, "broken.xlsx")
    assert len(sheets) == 1
    assert sheets[0].warnings, "an unreadable workbook must say so, not return silence"


def test_a_workbook_named_xls_is_profiled_too(tmp_path: Path):
    """The XP14 case end to end: D2A-46 identifies it, D2A-47 reads it."""
    path = _write(tmp_path / "legacy.xls", [["id", "sex"], [1, None]])

    detected, _ = formats.detect(path)
    assert detected.format_id == "xlsx"
    assert detected.extension_conflict is True
    assert detected.format_id in formats.WORKBOOK_FORMATS

    sheet = workbook.profile_workbook(path, "legacy.xls")[0]
    assert sheet.rows == 1


def test_ingest_profiles_workbooks_and_cites_the_sheet(tmp_path: Path):
    source = tmp_path / "ds"
    source.mkdir()
    _write(source / "animals.xlsx", [["id", "sex"], [1, "M"], [2, None]])

    result = ingest(source, tmp_path / "out")
    manifest = result.manifest if hasattr(result, "manifest") else None
    import json

    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    evidence = json.loads((tmp_path / "out" / "evidence.json").read_text(encoding="utf-8"))

    assert "animals.xlsx#Sheet1" in manifest["tables"], "a workbook must not yield zero tables"

    # The locator names the sheet, so a cell-level claim is checkable against a
    # multi-sheet file rather than only against the workbook as a whole.
    sheet_claims = [
        c
        for c in evidence["claims"]
        if any(e["check"].startswith("workbook.") for e in c["evidence"])
    ]
    assert sheet_claims
    located = [
        e
        for c in sheet_claims
        for e in c["evidence"]
        if isinstance(e.get("result"), dict) and "sheet" in e["result"]
    ]
    assert located and located[0]["result"]["sheet"] == "Sheet1"


def test_an_unreadable_workbook_makes_no_row_count_claim(tmp_path: Path):
    """An inability to profile must not become a positive finding.

    Previously the placeholder carried rows=0 and the pipeline recorded
    "sheet '' has 0 data row(s)" -- asserting a fact about content nobody read.
    """
    import json

    source = tmp_path / "ds"
    source.mkdir()
    with zipfile.ZipFile(source / "broken.xlsx", "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "not xml at all <<<")

    out = tmp_path / "out"
    ingest(source, out)
    evidence = json.loads((out / "evidence.json").read_text(encoding="utf-8"))

    checks = {e["check"] for c in evidence["claims"] for e in c["evidence"]}
    assert "workbook.unprofiled" in checks
    assert "workbook.row-count" not in checks
    assert not [c for c in evidence["claims"] if "0 data row" in c["claim"]]

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    profile = next(iter(manifest["tables"].values()))
    assert profile["profiled"] is False
    assert profile["rows"] is None, "an unobserved row count is null, never zero"


def test_a_worksheet_is_reachable_through_inspect_table(tmp_path: Path):
    """The sheet key is not an inventoried path; it must still resolve."""
    from data2agent.mcp.service import DatasetService

    source = tmp_path / "ds"
    source.mkdir()
    book = openpyxl.Workbook()
    book.active.title = "Sheet1"
    book.active.append(["id", "sex"])
    book.active.append([1, None])
    book.create_sheet("second").append(["x"])
    book.save(source / "a.xlsx")

    out = tmp_path / "out"
    ingest(source, out)
    service = DatasetService(out, mode="structured")

    profile = service.inspect_table("a.xlsx#Sheet1")
    assert profile["rows"] == 1
    assert profile["sheet"] == "Sheet1"
    # Integrity is checked against the backing workbook, not the synthetic key.
    assert profile["integrity"]["matches"] is True

    # Asking for the workbook itself points at its sheets rather than failing blankly.
    with pytest.raises(KeyError, match="holding 2 sheet"):
        service.inspect_table("a.xlsx")


def test_a_workbook_manifest_validates_against_the_published_schema(tmp_path: Path):
    """Every manifest holding a worksheet failed the schema before this."""
    import json

    jsonschema = pytest.importorskip("jsonschema")

    source = tmp_path / "ds"
    source.mkdir()
    _write(source / "a.xlsx", [["id", "sex"], [1, None]])

    out = tmp_path / "out"
    ingest(source, out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    schema = json.loads(Path("schemas/dataset-manifest.schema.json").read_text(encoding="utf-8"))

    jsonschema.validate(manifest, schema)


def test_header_disambiguation_is_unique_against_emitted_names(tmp_path: Path):
    """['id','id','id.1'] must not collapse two columns onto one name."""
    path = _write(tmp_path / "dup2.xlsx", [["id", "id", "id.1"], [1, 2, 3]])

    sheet = workbook.profile_workbook(path, "dup2.xlsx")[0]
    names = [c.name for c in sheet.columns]
    assert len(names) == len(set(names)), f"collision in {names}"
    # The name-keyed missing map would silently lose a column otherwise.
    assert len(sheet.as_dict()["missing"]) == len(names)


def test_a_sheet_that_fails_while_streaming_does_not_abort_the_ingest(tmp_path: Path):
    """read_only defers XML parsing, so a sheet can fail after a clean open."""
    good = _write(tmp_path / "ok.xlsx", [["id"], [1]])
    sheets = workbook.profile_workbook(good, "ok.xlsx")
    assert sheets[0].profiled is True

    # A workbook whose package opens but whose sheet XML is unusable.
    corrupt = tmp_path / "half.xlsx"
    with zipfile.ZipFile(good) as src_zip, zipfile.ZipFile(corrupt, "w") as dst:
        for item in src_zip.namelist():
            data = src_zip.read(item)
            if item.startswith("xl/worksheets/"):
                data = b"<worksheet><sheetData><row><c t=\x00"
            dst.writestr(item, data)

    result = workbook.profile_workbook(corrupt, "half.xlsx")
    assert result, "a failure must return a profile, not raise"
    assert all(s.profiled is False or s.rows is not None for s in result)
