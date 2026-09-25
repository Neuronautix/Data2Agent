"""Where a table's header is, and where its data begins (D2A-97).

Every profiler used to take the first non-empty row as the header. Real lab
files break that in three recurring ways, all found in the local screening
datasets:

1. a **banner** above the header -- a title, or a group label such as one cell
   reading "Weights" over the columns it covers -- so the banner becomes the
   header and the real column names become the first data row;
2. a **multi-row header** -- treatment names over a repeated metric row -- whose
   lower row alone yields ``Score``, ``Score.1``, ``Score.2`` with the treatment
   lost;
3. a **key: value preamble** before a delimited export's real header, as
   behavioural-scoring software writes it.

This module decides the header row by one explicit, deterministic rule, records
what it decided and why, and lets a person override it with a declaration. It
does not read column names for meaning, guess a locale, or learn anything: every
test below is a count or a cell's shape, and the rule is named and versioned so
a manifest says which rule produced its column names.

The rule (``RULE_ID``)
----------------------

Only the first ``_MAX_PREAMBLE_ROWS + 1`` non-blank rows are candidates. A row
is **header-like** when

* it has at least two non-blank cells,
* its non-blank cells are text -- not a number, a date or a boolean, and not a
  string that spells a number. A header may name a few columns with numbers
  (doses such as ``0.02``), so a row that is at least three-quarters text also
  qualifies, provided it is strictly more textual than the next non-blank row;
  otherwise a text-heavy data row would pass for a header,
* it is not a key-value label row (see below), and
* its non-blank cell count is more than half the widest non-blank count among
  it and the ``_BODY_LOOKAHEAD`` rows that follow it: a header is roughly as
  wide as the table it names.

A row is **preamble relative to a header** when it is sparse -- at most half as
many non-blank cells as the header, so a two-cell ``key<TAB>value`` line before
a four-column header qualifies -- or when it is a key-value label row: its
first non-blank cell is text ending in ``:`` (``Subjects:``, ``Observation
date:``).

The header is the **first header-like row**, provided every non-blank row above
it is preamble relative to it. The search stops at the first header-like row
whatever the verdict: letting it run on would let a narrow table at the top of
a sheet be skipped as "preamble" to a wider one further down.

* If the first header-like row is the first non-blank row, nothing changes:
  ``header_source`` is ``first-non-empty`` and the choice is ``confident``.
* If it is a later row and everything above it is preamble, those rows are
  skipped and listed with their row numbers and reasons, and ``header_source``
  is ``detected``.
* Otherwise the rule cannot establish a header, and the old behaviour applies:
  the first non-blank row, ``confident: false``. A warning is raised when that
  row itself looks like preamble, so the doubt is visible rather than silent.

Multi-row headers are **never composed by detection**. A sparse text row above
a header may be a group label or a title, and nothing structural tells the two
apart; composing on a guess would rename every column, which is worse than a
warning. When a skipped row sits directly above the chosen header, the warning
says so and names the declaration that would compose it.

Declarations
------------

A layout declaration (``data2agent ingest --layout layouts.json``) states the
header for named tables, and always wins over detection. It is validated
strictly: an unknown key, a malformed value, or a table path that matches no
profiled table is an error, never a silence. What detection would have chosen
is still recorded next to the declared choice, so an override is auditable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time
from itertools import chain
from pathlib import Path
from typing import Any

from ..errors import LayoutError
from .checksum import hash_file

RULE_ID = "d2a-header/1"

# Non-blank rows the rule may skip before the header. Bounded so that the choice
# is cheap on a huge file and cannot wander into a second table far below.
_MAX_PREAMBLE_ROWS = 15
# Rows after a candidate that are read to estimate the table's width.
_BODY_LOOKAHEAD = 5
# "Dense" and "sparse" are both measured against this fraction.
_DENSE_FRACTION = 0.5
# A header row with some numeric labels must still be at least this much text.
_MOSTLY_TEXT = 0.75
# A multi-row header deeper than this is almost certainly a mistake in the
# declaration (a data row number given as a row count).
_MAX_HEADER_ROWS = 10

_NUMBER = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")

SOURCE_FIRST_NON_EMPTY = "first-non-empty"
SOURCE_DETECTED = "detected"
SOURCE_DECLARED = "declared"

REASON_SPARSE = "sparse"
REASON_LABEL = "key-value-label"
REASON_DECLARED = "declared"

FILL_FORWARD = "forward"
FILL_NONE = "none"
_FILLS = (FILL_FORWARD, FILL_NONE)

LAYOUT_FORMAT_VERSION = "1"
_TABLE_KEYS = frozenset(
    {"header_row", "header_rows", "data_starts_row", "upper_label_fill", "note"}
)
_TOP_KEYS = frozenset({"layout_version", "layouts", "note"})


# --------------------------------------------------------------------------- rows


@dataclass(frozen=True)
class Row:
    """One row as its reader yields it, with the number a person would cite.

    ``number`` is the spreadsheet row for a sheet and, for a delimited file, the
    physical line on which the record *starts* -- a quoted cell can span lines.
    """

    number: int
    cells: tuple[Any, ...] | list[Any]


def text_of(value: Any) -> str:
    """The text a header cell contributes, stringified as the profilers do."""
    return "" if value is None else str(value)


def _non_blank(row: Row) -> int:
    return sum(1 for value in row.cells if text_of(value).strip())


def _is_blank(row: Row) -> bool:
    return not any(text_of(value).strip() for value in row.cells)


def _is_text(value: Any) -> bool:
    """A non-blank cell that is text, not a number, date or boolean in disguise."""
    if isinstance(value, (bool, int, float, datetime, date, time)):
        return False
    return not _NUMBER.match(text_of(value).strip())


def _first_cell(row: Row) -> Any:
    return next((value for value in row.cells if text_of(value).strip()), None)


def _is_label_row(row: Row) -> bool:
    first = _first_cell(row)
    return first is not None and _is_text(first) and text_of(first).strip().endswith(":")


# ---------------------------------------------------------------- declarations


@dataclass(frozen=True)
class TableLayout:
    """A declared header for one table. Row numbers are 1-based, as cited."""

    header_row: int
    header_rows: int = 1
    data_starts_row: int | None = None  # None: the row after the header block
    upper_label_fill: str = FILL_FORWARD
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"header_row": self.header_row, "header_rows": self.header_rows}
        if self.data_starts_row is not None:
            payload["data_starts_row"] = self.data_starts_row
        if self.header_rows > 1:
            payload["upper_label_fill"] = self.upper_label_fill
        if self.note is not None:
            payload["note"] = self.note
        return payload


@dataclass
class LayoutDeclarations:
    """A validated declaration file, and which of its tables were matched."""

    tables: dict[str, TableLayout]
    sha256: str
    name: str
    _used: set[str] = field(default_factory=set, repr=False)

    def for_table(self, path: str) -> TableLayout | None:
        layout = self.tables.get(path)
        if layout is not None:
            self._used.add(path)
        return layout

    def unmatched(self) -> list[str]:
        return sorted(set(self.tables) - self._used)

    def manifest_record(self) -> dict[str, Any]:
        """What the manifest carries: content identity only, never a local path.

        The digest is part of the manifest because the declaration is part of
        how the dataset was *read*; a relationships sidecar computed under one
        declaration must not survive a re-ingest under another.
        """
        return {"sha256": self.sha256, "tables": sorted(self.tables)}

    def provenance_record(self) -> dict[str, Any]:
        return {"name": self.name, "sha256": self.sha256, "tables": sorted(self.tables)}


def load_declarations(path: Path) -> LayoutDeclarations:
    """Read and strictly validate a layout declaration file."""
    path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise LayoutError(f"layout declaration {path.name!r} could not be read: {error}") from error
    except json.JSONDecodeError as error:
        raise LayoutError(f"layout declaration {path.name!r} is not valid JSON: {error}") from error
    return parse_declarations(payload, sha256=hash_file(path), name=path.name)


def parse_declarations(payload: Any, *, sha256: str, name: str) -> LayoutDeclarations:
    if not isinstance(payload, dict):
        raise LayoutError("a layout declaration must be a JSON object with a 'layouts' object")
    unknown = sorted(set(payload) - _TOP_KEYS)
    if unknown:
        raise LayoutError(
            f"layout declaration has unknown top-level key(s) {unknown}; "
            f"allowed: {sorted(_TOP_KEYS)}"
        )
    version = payload.get("layout_version", LAYOUT_FORMAT_VERSION)
    if str(version) != LAYOUT_FORMAT_VERSION:
        raise LayoutError(
            f"layout_version {version!r} is not supported; this version reads "
            f"{LAYOUT_FORMAT_VERSION!r}"
        )
    layouts = payload.get("layouts")
    if not isinstance(layouts, dict) or not layouts:
        raise LayoutError(
            "a layout declaration needs a non-empty 'layouts' object keyed by table path "
            "('<file>' or '<workbook>#<sheet>')"
        )
    tables = {str(key): _parse_table(str(key), value) for key, value in layouts.items()}
    return LayoutDeclarations(tables=tables, sha256=sha256, name=name)


def _parse_table(path: str, value: Any) -> TableLayout:
    if not path.strip():
        raise LayoutError("a layout declaration names an empty table path")
    if not isinstance(value, dict):
        raise LayoutError(f"layout for {path!r} must be an object")
    unknown = sorted(set(value) - _TABLE_KEYS)
    if unknown:
        raise LayoutError(
            f"layout for {path!r} has unknown key(s) {unknown}; allowed: {sorted(_TABLE_KEYS)}"
        )
    if "header_row" not in value:
        raise LayoutError(f"layout for {path!r} must state 'header_row'")

    header_row = _positive_int(path, "header_row", value["header_row"])
    header_rows = _positive_int(path, "header_rows", value.get("header_rows", 1))
    if header_rows > _MAX_HEADER_ROWS:
        raise LayoutError(
            f"layout for {path!r}: header_rows {header_rows} exceeds {_MAX_HEADER_ROWS}; "
            f"header_rows is a count of rows, not a row number"
        )
    data_starts_row = None
    if value.get("data_starts_row") is not None:
        data_starts_row = _positive_int(path, "data_starts_row", value["data_starts_row"])
        if data_starts_row < header_row + header_rows:
            raise LayoutError(
                f"layout for {path!r}: data_starts_row {data_starts_row} falls inside the "
                f"header (rows {header_row}..{header_row + header_rows - 1})"
            )
    fill = value.get("upper_label_fill", FILL_FORWARD)
    if fill not in _FILLS:
        raise LayoutError(
            f"layout for {path!r}: upper_label_fill must be one of {list(_FILLS)}, not {fill!r}"
        )
    note = value.get("note")
    if note is not None and not isinstance(note, str):
        raise LayoutError(f"layout for {path!r}: note must be a string")
    return TableLayout(header_row, header_rows, data_starts_row, fill, note)


def _positive_int(path: str, key: str, value: Any) -> int:
    # bool is an int subclass; 'true' as a row number is a typo, not a row.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LayoutError(f"layout for {path!r}: {key} must be a positive integer, not {value!r}")
    return value


# ------------------------------------------------------------------- the result


@dataclass
class HeaderLayout:
    """What was decided about one table's header, and on what basis."""

    source: str | None  # None only when the table has no non-blank row at all
    header_row: int | None
    header_rows: int
    data_starts_row: int | None
    confident: bool
    detected_header_row: int | None
    skipped_rows: list[dict[str, Any]]
    labels: list[str]  # one composed label per column; "" where the header is blank
    header_cells: list[list[str]] | None  # per column, the header block's own cells
    declared: TableLayout | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """The manifest fields every table profile carries, in a fixed order."""
        detection: dict[str, Any] = {
            "rule": RULE_ID,
            "confident": self.confident,
            "detected_header_row": self.detected_header_row,
            "skipped_rows": [dict(item) for item in self.skipped_rows],
        }
        if self.declared is not None:
            detection["declared"] = self.declared.as_dict()
        return {
            "header_row": self.header_row,
            "header_rows": self.header_rows,
            "data_starts_row": self.data_starts_row,
            "header_source": self.source,
            "header_detection": detection,
        }


# ------------------------------------------------------------------ resolution


def split_header(
    rows: Iterable[Row], declared: TableLayout | None = None
) -> tuple[HeaderLayout, Iterator[Row]]:
    """Decide the header of a table and return the rows that follow it.

    ``rows`` is consumed lazily: only the head of the table -- enough rows for
    the rule, or to reach a declared data start -- is buffered, so a large file
    is still streamed. The returned iterator yields every row numbered at or
    after ``data_starts_row``, blank rows included; whether a blank row counts as
    an observation is the caller's convention, not this module's.
    """
    iterator = iter(rows)
    head: list[Row] = []
    needed_non_blank = _MAX_PREAMBLE_ROWS + 1 + _BODY_LOOKAHEAD
    needed_row = 0
    if declared is not None:
        needed_row = max(
            declared.header_row + declared.header_rows + _BODY_LOOKAHEAD,
            declared.data_starts_row or 0,
        )
    non_blank = 0
    for row in iterator:
        head.append(row)
        if not _is_blank(row):
            non_blank += 1
        if non_blank >= needed_non_blank and row.number >= needed_row:
            break

    detected = _detect(head)
    layout = _declared(head, declared, detected) if declared is not None else detected
    if layout.data_starts_row is None:
        return layout, iter(())
    start = layout.data_starts_row
    return layout, chain((row for row in head if row.number >= start), iterator)


def _detect(head: list[Row]) -> HeaderLayout:
    rows = [row for row in head if not _is_blank(row)]
    if not rows:
        return HeaderLayout(
            source=None,
            header_row=None,
            header_rows=0,
            data_starts_row=None,
            confident=True,
            detected_header_row=None,
            skipped_rows=[],
            labels=[],
            header_cells=None,
        )

    counts = [_non_blank(row) for row in rows]
    chosen: int | None = None
    for index in range(min(len(rows), _MAX_PREAMBLE_ROWS + 1)):
        if not _header_like(rows, counts, index):
            continue
        if all(_preamble_reason(rows[j], counts[j], counts[index]) for j in range(index)):
            chosen = index
        break  # the first header-like row decides; see the module docstring

    warnings: list[str] = []
    if chosen is None:
        index, confident, skipped = 0, False, []
        first = rows[0]
        width = max(counts[: 1 + _BODY_LOOKAHEAD])
        if _is_label_row(first) or counts[0] <= _DENSE_FRACTION * width:
            warnings.append(
                f"row {first.number} was taken as the header because it is the first non-empty "
                f"row, but it looks like a title or preamble line and no later row could be "
                f"established as the header under rule {RULE_ID}; declare the layout "
                f"(--layout) if the column names are wrong"
            )
    else:
        index, confident = chosen, True
        skipped = [
            {
                "row": rows[j].number,
                "reason": _preamble_reason(rows[j], counts[j], counts[index]),
                "non_empty_cells": counts[j],
            }
            for j in range(index)
        ]

    header = rows[index]
    layout = _finish(
        head,
        header_row=header.number,
        header_rows=1,
        data_starts_row=None,
        fill=FILL_NONE,
        source=SOURCE_DETECTED if skipped else SOURCE_FIRST_NON_EMPTY,
        confident=confident,
        skipped=skipped,
    )
    layout.detected_header_row = header.number
    layout.warnings.extend(warnings)
    if skipped:
        numbers = ", ".join(str(item["row"]) for item in skipped)
        note = (
            f"header taken from row {header.number}, not the first non-empty row "
            f"{rows[0].number}: row(s) {numbers} skipped as preamble under rule {RULE_ID}"
        )
        above = rows[index - 1]
        if skipped[-1]["reason"] == REASON_SPARSE and _adjacent(head, above, header):
            note += (
                f". Row {above.number} sits directly above the header; if it labels groups "
                f"of columns, declare header_row {above.number} with header_rows 2 to keep "
                f"those labels in the column names"
            )
        layout.warnings.append(note)
    return layout


def _text_fraction(row: Row) -> float:
    values = [value for value in row.cells if text_of(value).strip()]
    return sum(1 for value in values if _is_text(value)) / len(values) if values else 0.0


def _header_like(rows: list[Row], counts: list[int], index: int) -> bool:
    row = rows[index]
    if counts[index] < 2 or _is_label_row(row):
        return False
    fraction = _text_fraction(row)
    if fraction < 1.0:
        # A header may name a few columns with numbers (doses: 0.02, 0.07), so
        # it need only be mostly text -- but then it must also be more textual
        # than the row below it, or a text-heavy data row would qualify.
        following = _text_fraction(rows[index + 1]) if index + 1 < len(rows) else 0.0
        if fraction < _MOSTLY_TEXT or fraction <= following:
            return False
    width = max(counts[index : index + 1 + _BODY_LOOKAHEAD])
    return counts[index] > _DENSE_FRACTION * width


def _preamble_reason(row: Row, count: int, header_count: int) -> str | None:
    if _is_label_row(row):
        return REASON_LABEL
    if count <= _DENSE_FRACTION * header_count:
        return REASON_SPARSE
    return None


def _adjacent(head: list[Row], upper: Row, lower: Row) -> bool:
    """Whether no row at all -- not even a blank one -- separates the two."""
    position = next(i for i, row in enumerate(head) if row is upper)
    return position + 1 < len(head) and head[position + 1] is lower


def _declared(head: list[Row], declared: TableLayout, detected: HeaderLayout) -> HeaderLayout:
    by_number = {row.number: row for row in head}
    if declared.header_row not in by_number:
        last = head[-1].number if head else 0
        raise LayoutError(
            f"declared header_row {declared.header_row} is not a row of this table "
            f"(its last row read is {last}; for a delimited file it must be the line on "
            f"which a record starts)"
        )
    skipped = [
        {"row": row.number, "reason": REASON_DECLARED, "non_empty_cells": _non_blank(row)}
        for row in head
        if row.number < declared.header_row and not _is_blank(row)
    ]
    layout = _finish(
        head,
        header_row=declared.header_row,
        header_rows=declared.header_rows,
        data_starts_row=declared.data_starts_row,
        fill=declared.upper_label_fill,
        source=SOURCE_DECLARED,
        confident=True,
        skipped=skipped,
    )
    layout.detected_header_row = detected.detected_header_row
    layout.declared = declared
    return layout


def _finish(
    head: list[Row],
    *,
    header_row: int,
    header_rows: int,
    data_starts_row: int | None,
    fill: str,
    source: str,
    confident: bool,
    skipped: list[dict[str, Any]],
) -> HeaderLayout:
    start = next(i for i, row in enumerate(head) if row.number == header_row)
    block = head[start : start + header_rows]
    if len(block) < header_rows:
        raise LayoutError(
            f"declared header_rows {header_rows} from row {header_row} runs past the end "
            f"of the table"
        )
    after = head[start + header_rows] if start + header_rows < len(head) else None
    if data_starts_row is None:
        # Directly after the header block. At the end of the table this is still
        # a row number -- one past the last -- so readers yield nothing.
        data_starts_row = after.number if after is not None else block[-1].number + 1
    else:
        skipped = skipped + [
            {"row": row.number, "reason": REASON_DECLARED, "non_empty_cells": _non_blank(row)}
            for row in head[start + header_rows :]
            if row.number < data_starts_row and not _is_blank(row)
        ]
    labels, cells = compose_labels([list(row.cells) for row in block], fill)
    return HeaderLayout(
        source=source,
        header_row=header_row,
        header_rows=header_rows,
        data_starts_row=data_starts_row,
        confident=confident,
        detected_header_row=None,
        skipped_rows=skipped,
        labels=labels,
        header_cells=cells if header_rows > 1 else None,
    )


def compose_labels(block: list[list[Any]], fill: str) -> tuple[list[str], list[list[str]]]:
    """One label per column from a header block, upper rows first, joined by ' / '.

    With ``fill='forward'`` a blank cell in an *upper* row takes the label to its
    left -- the reading of a label merged or centred across a group of columns --
    unless a row above starts a new group at that column, or nothing below it in
    the block names the column. The lowest row is never filled. The cells as
    written are returned alongside, so every filled label stays auditable.
    """
    width = max((len(row) for row in block), default=0)
    grid = [
        [text_of(row[c]).strip() if c < len(row) else "" for c in range(width)] for row in block
    ]
    filled = [list(row) for row in grid]
    if fill == FILL_FORWARD:
        for level in range(len(grid) - 1):
            for column in range(1, width):
                if grid[level][column]:
                    continue
                if any(grid[upper][column] for upper in range(level)):
                    continue  # a new group starts above: the label to the left ends here
                if not any(grid[lower][column] for lower in range(level + 1, len(grid))):
                    continue  # nothing below names this column: the edge of the header
                filled[level][column] = filled[level][column - 1]
    labels = [
        " / ".join(part for part in (filled[level][column] for level in range(len(grid))) if part)
        for column in range(width)
    ]
    cells = [[grid[level][column] for level in range(len(grid))] for column in range(width)]
    return labels, cells
