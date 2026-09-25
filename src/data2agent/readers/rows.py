"""Bounded row readers for the queryable scientific data plane.

These readers expose observations without adding semantics. They reuse the
recorded table profile (columns, delimiter/header, missing-value convention)
instead of re-discovering structure at query time, and callers must verify the
backing file's checksum before invoking them.

Returned values are JSON-safe. Missing sentinels are normalised to null under
the same named convention used during ingest, while the raw sentinel is retained
in the per-row missing map so no source token is erased without an audit trail.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from ..ingest.conventions import SENTINEL, MissingValueConvention
from ..ingest.layout import column_index
from ..ingest.tabular import numbered_records
from . import workbook


def read_delimited_rows(
    path: Path,
    profile: dict[str, Any],
    *,
    columns: list[str],
    offset: int,
    limit: int,
    convention: MissingValueConvention,
) -> list[dict[str, Any]]:
    """Read a bounded slice from a profiled delimited table."""
    encoding = profile.get("encoding") or "utf-8"
    delimiter = profile["delimiter"]
    specs = _column_specs(profile, columns)

    data_starts_row = profile.get("data_starts_row")
    if data_starts_row is None and profile.get("header_source") is not None:
        return []  # a header was decided and nothing follows it
    last_row, span = block_bounds(profile)

    rows: list[dict[str, Any]] = []
    data_index = 0
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        if data_starts_row is None:
            # Manifests written before D2A-97 took the first record as the
            # header, whatever it held; honour exactly that reading.
            try:
                next(reader)
            except StopIteration:
                return []
            records = numbered_records(reader)
        else:
            # Records numbered by the line they start on, exactly as the
            # profiler numbered them: preamble, banner and every header row sit
            # before data_starts_row and are skipped, and source_row below stays
            # the file's own line number.
            start = int(data_starts_row)
            records = (row for row in numbered_records(reader) if row.number >= start)

        for numbered in records:
            if last_row is not None and numbered.number > last_row:
                break  # the end of a declared block: the rows below are another table
            record = numbered.cells
            if not record:
                continue  # match the profiler: blank lines carry no observation
            if span is not None and not any(
                str(cell).strip() for cell in record[span[0] : span[1] + 1]
            ):
                continue  # blank within the block's columns, as the profiler skips it
            if data_index < offset:
                data_index += 1
                continue
            if len(rows) >= limit:
                break

            values, missing = _project(record, specs, convention)
            rows.append(
                {
                    # csv.reader.line_num is the physical line on which this
                    # logical record ends; quoted multiline cells can span lines.
                    "source_row": reader.line_num,
                    "values": values,
                    "missing": missing,
                }
            )
            data_index += 1
    return rows


def read_workbook_rows(
    path: Path,
    profile: dict[str, Any],
    *,
    columns: list[str],
    offset: int,
    limit: int,
    convention: MissingValueConvention,
) -> list[dict[str, Any]]:
    """Read a bounded slice from one profiled worksheet, with the backend that profiled it."""
    header_row = profile.get("header_row")
    if header_row is None:
        return []
    # The header may span several rows, and a declaration may skip rows below
    # it; data_starts_row says where observations begin. Manifests written
    # before D2A-97 lack it and had a one-row header, so the row below it.
    data_starts_row = profile.get("data_starts_row")
    first_data_row = int(data_starts_row) if data_starts_row is not None else int(header_row) + 1

    sheet_name, backend = _sheet_and_backend(profile)
    specs = _column_specs(profile, columns)
    last_row, _ = block_bounds(profile)
    rows: list[dict[str, Any]] = []
    with workbook.open_workbook(path, backend) as book:
        start_row = first_data_row + offset
        for source_row, record in enumerate(
            book.iter_rows(sheet_name, min_row=start_row), start=start_row
        ):
            if len(rows) >= limit:
                break
            if last_row is not None and source_row > last_row:
                break  # the end of a declared block: the rows below are another table
            values, missing = _project(record, specs, convention)
            rows.append(
                {
                    "source_row": source_row,
                    "values": values,
                    "missing": missing,
                }
            )
    return rows


def block_bounds(profile: dict[str, Any]) -> tuple[int | None, tuple[int, int] | None]:
    """A declared block's last row and inclusive column positions (D2A-103).

    ``(None, None)`` for every table that is not a block, which is read as
    before: to the end of its file or sheet, across every column.
    """
    block = profile.get("block")
    if not isinstance(block, dict):
        return None, None
    last_row = block.get("last_row")
    span = None
    spec = block.get("columns")
    if isinstance(spec, str) and ":" in spec:
        first, last = spec.split(":", 1)
        span = (column_index(first), column_index(last))
    return (int(last_row) if last_row is not None else None), span


def _sheet_and_backend(profile: dict[str, Any]) -> tuple[str, workbook.Backend]:
    sheet_name = profile.get("sheet")
    if not sheet_name:
        raise KeyError("worksheet profile does not record a sheet name")
    # Manifests written before D2A-94 carry no reader block; they were all
    # profiled with openpyxl, which is therefore the only faithful default.
    backend_name = (profile.get("reader") or {}).get("backend", workbook.OPENPYXL.name)
    backend = workbook.BACKENDS.get(backend_name)
    if backend is None:
        raise KeyError(f"worksheet was profiled by an unknown reader {backend_name!r}")
    return sheet_name, backend


def rows_above_data(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """The non-blank rows above a table's data that its column names do not show.

    Taken from the profile, never re-detected: the rows skipped as preamble,
    banner or declared gap, plus -- for a multi-row header only -- the header
    rows themselves, whose cells the composed names abbreviate. A one-row
    header is fully visible as the column names, so a clean table lists none.
    Counts only; the cells are read from the verified file by
    :func:`read_rows_above_data`.
    """
    detection = profile.get("header_detection") or {}
    listed: dict[tuple[int, int], dict[str, Any]] = {}
    for item in detection.get("skipped_rows") or []:
        listed[(int(item["row"]), 0)] = {
            "row": int(item["row"]),
            "role": "skipped",
            "reason": item.get("reason"),
        }
    header_row = profile.get("header_row")
    header_rows = int(profile.get("header_rows") or 0)
    if header_row is not None and header_rows > 1:
        start = int(header_row)
        for offset in range(header_rows):
            # A header is `header_rows` consecutive *records*. In a sheet a record
            # is a row, so its number is start + offset. In a delimited file a
            # quoted cell can span lines, so only the first record's line is
            # known here; the rest are numbered by the reader, from the file.
            known = offset == 0 or bool(profile.get("workbook"))
            listed[(start, offset)] = {
                "row": start + offset if known else None,
                "role": "header",
                "reason": None,
                "header_offset": offset,
            }
    return [listed[key] for key in sorted(listed)]


def read_rows_above_data(
    path: Path,
    profile: dict[str, Any],
    *,
    max_rows: int,
    max_cells: int,
    convention: MissingValueConvention,
) -> dict[str, Any]:
    """Read the cells of :func:`rows_above_data`, bounded, from the verified file.

    Cells are normalised exactly as :func:`read_delimited_rows` and
    :func:`read_workbook_rows` normalise an untyped cell: a blank is skipped, a
    token the convention resolves is null with its raw form kept, a date is ISO
    8601, and a sheet number stays a number. No row carries a dtype, because
    none was profiled for it, so a delimited cell stays the text the file holds.
    Only non-blank cells are returned, each with its 0-based position, the
    spreadsheet column letter, and the name of the table column at that
    position when there is one.
    """
    wanted = rows_above_data(profile)
    _, span = block_bounds(profile)
    selected = [dict(item) for item in wanted[:max_rows]]
    numbers = {item["row"] for item in selected if item["row"] is not None}
    offsets = {item["header_offset"] for item in selected if item["row"] is None}
    raw: dict[int, Any] = {}
    if numbers or offsets:
        if profile.get("workbook"):
            sheet_name, backend = _sheet_and_backend(profile)
            first, last = min(numbers), max(numbers)
            with workbook.open_workbook(path, backend) as book:
                for number, record in enumerate(
                    book.iter_rows(sheet_name, min_row=first), start=first
                ):
                    if number > last:
                        break
                    if number in numbers:
                        raw[number] = record
        else:
            encoding = profile.get("encoding") or "utf-8"
            last = max(numbers)
            header_start = int(profile["header_row"]) if offsets else None
            header_lines: dict[int, int] = {}  # header offset -> line the record starts on
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.reader(handle, delimiter=profile["delimiter"])
                in_header: int | None = None
                for numbered in numbered_records(reader):
                    if numbered.number == header_start:
                        in_header = 0
                    if in_header is not None:
                        if in_header in offsets:
                            header_lines[in_header] = numbered.number
                            raw[numbered.number] = numbered.cells
                        in_header += 1
                    if numbered.number in numbers:
                        raw[numbered.number] = numbered.cells
                    if numbered.number >= last and len(header_lines) == len(offsets):
                        break
            for item in selected:
                if item["row"] is None:
                    item["row"] = header_lines.get(item["header_offset"])

    names = {int(column["position"]): column["name"] for column in profile.get("columns", [])}
    rows: list[dict[str, Any]] = []
    for item in selected:
        item.pop("header_offset", None)
        record = raw.get(item["row"], ()) if item["row"] is not None else ()
        cells: list[dict[str, Any]] = []
        non_blank = 0
        for position, value in enumerate(record):
            if span is not None and not span[0] <= position <= span[1]:
                continue  # outside a declared block's columns: another table's cells
            normalised, reason = _normalise(value, "string", convention)
            if reason is not None and reason["kind"] == "empty":
                continue
            non_blank += 1
            if len(cells) >= max_cells:
                continue
            cell: dict[str, Any] = {
                "position": position,
                "column_letter": workbook.column_letter(position),
                "table_column": names.get(position),
                "value": normalised,
            }
            if reason is not None:
                cell["missing"] = reason
            cells.append(cell)
        rows.append(
            {
                **item,
                "cells": cells,
                "non_empty_cells": non_blank,
                "cells_truncated": non_blank > len(cells),
            }
        )
    return {
        "rows_above_data": rows,
        "rows_available": len(wanted),
        "rows_truncated": len(wanted) > len(selected),
    }


def _column_specs(profile: dict[str, Any], columns: list[str]) -> list[tuple[str, int, str]]:
    by_name = {column["name"]: column for column in profile.get("columns", [])}
    specs: list[tuple[str, int, str]] = []
    for name in columns:
        column = by_name[name]
        specs.append((name, int(column["position"]), str(column.get("dtype") or "string")))
    return specs


def _project(
    record: Any,
    specs: list[tuple[str, int, str]],
    convention: MissingValueConvention,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    values: dict[str, Any] = {}
    missing: dict[str, dict[str, Any]] = {}
    for name, position, dtype in specs:
        raw = record[position] if position < len(record) else None
        value, reason = _normalise(raw, dtype, convention)
        values[name] = value
        if reason is not None:
            missing[name] = reason
    return values, missing


def _normalise(
    raw: Any, dtype: str, convention: MissingValueConvention
) -> tuple[Any, dict[str, Any] | None]:
    if raw is None:
        return None, {"kind": "empty"}

    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped == "":
            return None, {"kind": "empty"}
        if convention.classify(stripped) == SENTINEL:
            return None, {"kind": "sentinel", "raw": raw}

        # Coercion follows the dtype already established during deterministic
        # profiling; query-time access does not infer a new type.
        if dtype == "integer":
            try:
                return int(stripped), None
            except ValueError:
                return raw, None
        if dtype == "number":
            try:
                return float(stripped), None
            except ValueError:
                return raw, None
        if dtype == "boolean":
            lowered = stripped.lower()
            if lowered in {"true", "false"}:
                return lowered == "true", None
        return raw, None

    if isinstance(raw, (datetime, date, time)):
        return raw.isoformat(), None
    if isinstance(raw, (bool, int, float)):
        return raw, None

    # A parser can surface a small number of scalar-like Python objects that are
    # not JSON serialisable. Preserve their textual value rather than failing or
    # inventing a richer interpretation.
    return str(raw), None
