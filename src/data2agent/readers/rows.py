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

    rows: list[dict[str, Any]] = []
    data_index = 0
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        try:
            next(reader)  # the profiled header
        except StopIteration:
            return []

        for record in reader:
            if not record:
                continue  # match the profiler: blank lines carry no observation
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

    sheet_name = profile.get("sheet")
    if not sheet_name:
        raise KeyError("worksheet profile does not record a sheet name")

    # Manifests written before D2A-94 carry no reader block; they were all
    # profiled with openpyxl, which is therefore the only faithful default.
    backend_name = (profile.get("reader") or {}).get("backend", workbook.OPENPYXL.name)
    backend = workbook.BACKENDS.get(backend_name)
    if backend is None:
        raise KeyError(f"worksheet was profiled by an unknown reader {backend_name!r}")

    specs = _column_specs(profile, columns)
    rows: list[dict[str, Any]] = []
    with workbook.open_workbook(path, backend) as book:
        start_row = int(header_row) + 1 + offset
        for source_row, record in enumerate(
            book.iter_rows(sheet_name, min_row=start_row), start=start_row
        ):
            if len(rows) >= limit:
                break
            values, missing = _project(record, specs, convention)
            rows.append(
                {
                    "source_row": source_row,
                    "values": values,
                    "missing": missing,
                }
            )
    return rows


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
