"""OOXML workbook profiling.

The deterministic core profiles delimited text only. A real preclinical dataset
keeps its numbers in spreadsheets, so a core that cannot open a workbook reports
``tables: 0`` on a dataset full of tables -- and every downstream check that
depends on cell-level facts abstains for the wrong reason. That is D2A-47, found
by the XP14 run (#16).

Three commitments shape this module.

**The shape is observed, not interpreted.** A sheet's used range is a rectangle
openpyxl reports; this module records it and profiles what is inside. It does
not decide which sheet is "the data", does not merge multi-row headers, and does
not infer meaning from a column name.

**The header is an assumption, and it is recorded as one.** The first non-empty
row of the used range is taken as the header, exactly as the delimited profiler
treats a first line. Unlike a CSV, a sheet often has titles, merged banners or
blank rows above the real header -- XP14's own registry starts on row 2 -- so the
row that was used is reported in ``header_row`` and anomalies raise warnings.
The assumption is then auditable rather than invisible.

**Missingness is resolved by the same machinery as CSV.** ``_observe`` from the
delimited profiler is reused deliberately, so a token means the same thing in a
sheet as in a CSV and ``manifest.missing_value_convention`` governs both. Two
implementations would let the convention drift, and the convention is the thing
that makes a missingness count reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..ingest.conventions import DEFAULT_CONVENTION, MissingValueConvention
from ..ingest.tabular import ColumnProfile, _convention_warnings, _observe, finalise, new_column

# Cap on sheets profiled per workbook. A pathological file should slow an
# ingest, not hang it; the cap is reported rather than applied silently.
_MAX_SHEETS = 64


class WorkbookReaderUnavailable(RuntimeError):
    """Raised when the optional parser is not installed."""


@dataclass
class SheetProfile:
    """Observed properties of one worksheet."""

    path: str  # "<workbook path>#<sheet name>"
    workbook: str
    sheet: str
    sheet_index: int
    sheet_state: str  # "visible" | "hidden" | "veryHidden"
    header_row: int | None
    has_header: bool
    # None, never 0, when the content was not observed. Zero rows is a finding
    # about a sheet that was read; an unreadable file has no row count at all,
    # and reporting one turns an inability to profile into false evidence.
    profiled: bool
    rows: int | None
    columns: list[ColumnProfile]
    merged_ranges: int
    convention: MissingValueConvention
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "workbook": self.workbook,
            "sheet": self.sheet,
            "sheet_index": self.sheet_index,
            "sheet_state": self.sheet_state,
            "header_row": self.header_row,
            "has_header": self.has_header,
            "profiled": self.profiled,
            "rows": self.rows,
            "column_count": len(self.columns),
            "merged_ranges": self.merged_ranges,
            "missing_convention": self.convention.as_dict(),
            "columns": [column.as_dict() for column in self.columns],
            "missing": {column.name: column.missing for column in self.columns},
            "warnings": list(self.warnings),
        }


def available() -> bool:
    """Whether the optional parser is importable."""
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return False
    return True


def profile_workbook(
    path: Path,
    relative_path: str,
    convention: MissingValueConvention = DEFAULT_CONVENTION,
) -> list[SheetProfile]:
    """Profile every worksheet in an OOXML workbook.

    Raises :class:`WorkbookReaderUnavailable` when the optional parser is
    missing, so the caller can report a workbook it could not read instead of
    reporting a dataset with no tables.
    """
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - exercised via available()
        raise WorkbookReaderUnavailable(
            "reading OOXML workbooks requires the 'xlsx' extra: pip install 'data2agent[xlsx]'"
        ) from exc

    # Opened from a file handle, not a path: openpyxl dispatches on the
    # extension and refuses an OOXML workbook named '.xls', which is the case
    # D2A-46 identified in XP14. A handle is both extension-independent and
    # seekable, so read_only streaming still applies -- reading the archive into
    # memory first would defeat it on exactly the large files it protects.
    handle = path.open("rb")
    try:
        book = openpyxl.load_workbook(
            handle,
            read_only=True,  # streaming: a large workbook must not be held whole
            data_only=True,  # cached values, not formula text; see module note
        )
    except Exception as exc:  # openpyxl raises a wide range on malformed input
        handle.close()
        return [_unreadable(relative_path, convention, f"{type(exc).__name__}: {exc}")]

    profiles: list[SheetProfile] = []
    try:
        sheet_names = list(book.sheetnames)
        for index, name in enumerate(sheet_names):
            if index >= _MAX_SHEETS:
                profiles.append(
                    _unreadable(
                        relative_path,
                        convention,
                        f"workbook has {len(sheet_names)} sheets; "
                        f"only the first {_MAX_SHEETS} were profiled",
                    )
                )
                break
            try:
                profiles.append(_profile_sheet(book[name], relative_path, index, convention))
            except Exception as exc:
                # read_only defers XML parsing until the rows are iterated, so a
                # sheet can fail long after the workbook opened cleanly. One bad
                # sheet must not abort the ingest of the whole dataset.
                profiles.append(
                    _unreadable(
                        f"{relative_path}#{name}",
                        convention,
                        f"sheet '{name}' could not be read: {type(exc).__name__}: {exc}",
                        workbook=relative_path,
                        sheet=name,
                    )
                )
    finally:
        book.close()
        handle.close()

    return profiles


def _unreadable(
    relative_path: str,
    convention: MissingValueConvention,
    note: str,
    *,
    workbook: str | None = None,
    sheet: str = "",
) -> SheetProfile:
    """A placeholder recording that a workbook exists and was not profiled.

    Emitted rather than returning nothing, because an absent table and an
    unreadable one are different facts and only one of them is a data quality
    finding about the dataset.
    """
    return SheetProfile(
        path=relative_path,
        workbook=workbook if workbook is not None else relative_path,
        sheet=sheet,
        sheet_index=-1,
        sheet_state="unknown",
        header_row=None,
        has_header=False,
        profiled=False,
        rows=None,
        columns=[],
        merged_ranges=0,
        convention=convention,
        warnings=[note],
    )


def _profile_sheet(sheet, workbook_path: str, index: int, convention: MissingValueConvention):
    warnings: list[str] = []
    name = sheet.title
    state = getattr(sheet, "sheet_state", "visible") or "visible"
    if state != "visible":
        warnings.append(
            f"sheet is '{state}'; profiled anyway, because a hidden sheet is "
            f"still data the file carries"
        )

    # read_only worksheets expose merged ranges inconsistently; absence is not
    # evidence of none, so it is reported as 0 without claiming certainty.
    merged = len(getattr(sheet, "merged_cells", None).ranges) if _has_merged(sheet) else 0

    rows_iter = sheet.iter_rows(values_only=True)
    header_row_index: int | None = None
    header: list[str] = []
    columns: list[ColumnProfile] = []
    data_rows = 0

    for row_number, raw_row in enumerate(rows_iter, start=1):
        cells = ["" if v is None else str(v) for v in raw_row]
        if header_row_index is None:
            if not any(cell.strip() for cell in cells):
                continue  # leading blank rows are layout, not data
            header_row_index = row_number
            header = _header_names(cells, warnings, name, row_number)
            columns = [new_column(n, i) for i, n in enumerate(header)]
            continue

        data_rows += 1
        if len(cells) > len(columns):
            # Cells to the right of the header row. Recorded, never discarded:
            # a header narrower than its data is a finding about the sheet.
            for extra in range(len(columns), len(cells)):
                columns.append(new_column(_column_letter(extra), extra))
                columns[-1].missing_empty += data_rows - 1
        for position, column in enumerate(columns):
            _observe(column, cells[position] if position < len(cells) else "", convention)

    # Finalised by the delimited profiler's own routine, so the invariant
    # missing == missing_empty + missing_sentinel, and the uniqueness verdict a
    # metadata rule reads, are computed identically for a sheet and for a CSV.
    for column in columns:
        finalise(column, data_rows)

    warnings.extend(_convention_warnings(columns, convention))

    if header_row_index is None:
        warnings.append("sheet is empty; no header row and no data rows")

    return SheetProfile(
        path=f"{workbook_path}#{name}",
        workbook=workbook_path,
        sheet=name,
        sheet_index=index,
        sheet_state=state,
        header_row=header_row_index,
        has_header=header_row_index is not None,
        profiled=True,
        rows=data_rows,
        columns=columns,
        merged_ranges=merged,
        convention=convention,
        warnings=warnings,
    )


def _has_merged(sheet) -> bool:
    merged = getattr(sheet, "merged_cells", None)
    return merged is not None and hasattr(merged, "ranges")


def _header_names(cells: list[str], warnings: list[str], sheet: str, row: int) -> list[str]:
    """Name the columns from the header row, filling blanks positionally.

    A blank header cell is named for its spreadsheet column so that the column
    is still profiled. Silently dropping it would under-report the sheet's width.
    """
    names: list[str] = []
    blanks = 0
    # Tracks what has been EMITTED, not what was read. Suffixing against the
    # original labels lets ['id', 'id', 'id.1'] collapse to two identical
    # 'id.1' entries, which then collide in the name-keyed missing map.
    emitted: set[str] = set()
    for position, cell in enumerate(cells):
        label = cell.strip()
        if not label:
            blanks += 1
            label = _column_letter(position)
        if label in emitted:
            base, suffix = label, 1
            while f"{base}.{suffix}" in emitted:
                suffix += 1
            label = f"{base}.{suffix}"
        emitted.add(label)
        names.append(label)

    while names and names[-1] == _column_letter(len(names) - 1):
        names.pop()  # trailing empties are the edge of the used range, not columns

    if blanks:
        warnings.append(
            f"header row {row} has {blanks} blank cell(s); those columns are named "
            f"for their spreadsheet column letter. A sheet whose real header is not "
            f"its first non-empty row will be mis-labelled here -- the row used is "
            f"recorded in 'header_row'"
        )
    return names


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters
