"""Delimited-table profiling.

This module answers only questions the bytes can answer, plus one question the
bytes cannot: whether a token such as ``NA`` means "missing". That question is
resolved by an explicit :mod:`~data2agent.ingest.conventions` record rather than
by judgement, the convention travels with the profile, and every missingness
claim cites it. Change the convention and the numbers change, visibly.

What is still never decided here: what a column *means*. ``birth_date`` holds
string-shaped tokens because nothing declares a date format, and no column
acquires units, a controlled term or a scientific type from this module.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from .conventions import AMBIGUOUS, DEFAULT_CONVENTION, SENTINEL, MissingValueConvention
from .textio import read_text

# Delimiters we are willing to consider, in preference order.
_CANDIDATE_DELIMITERS = (",", "\t", ";", "|")
_EXTENSION_DELIMITERS = {".csv": ",", ".tsv": "\t", ".tab": "\t"}
_SNIFF_LINES = 20

_INTEGER = re.compile(r"^[+-]?\d+$")
_NUMBER = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")
_BOOLEAN = {"true", "false"}

# Distinct values are only enumerated below this cardinality; above it a column
# is treated as free-form and only an upper bound on the count is kept.
_MAX_ENUMERATED_DISTINCT = 25


@dataclass
class ColumnProfile:
    """Observed properties of one column, under a named missing-value convention."""

    name: str
    position: int
    dtype: str = "empty"
    values: int = 0
    missing: int = 0
    missing_empty: int = 0
    missing_sentinel: int = 0
    sentinel_tokens_seen: dict[str, int] = field(default_factory=dict)
    ambiguous_tokens_seen: dict[str, int] = field(default_factory=dict)
    distinct: int = 0
    distinct_exact: bool = True
    distinct_values: list[str] | None = None
    _seen: set[str] = field(default_factory=set, repr=False)
    _overflowed: bool = field(default=False, repr=False)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "position": self.position,
            "dtype": self.dtype,
            "values": self.values,
            # missing == missing_empty + missing_sentinel, always.
            "missing": self.missing,
            "missing_empty": self.missing_empty,
            "missing_sentinel": self.missing_sentinel,
            "sentinel_tokens_seen": dict(sorted(self.sentinel_tokens_seen.items())),
            # Observed, deliberately NOT resolved to missing. A cell reading
            # 'unknown' may be a considered statement rather than an absence.
            "ambiguous_tokens_seen": dict(sorted(self.ambiguous_tokens_seen.items())),
            "distinct": self.distinct,
            # Above the enumeration cap we stop tracking the value set, so the
            # count becomes an upper bound. Saying which it is keeps the number
            # a supportable claim rather than a plausible-looking one.
            "distinct_exact": self.distinct_exact,
        }
        if self.distinct_values is not None:
            payload["distinct_values"] = self.distinct_values
        return payload


@dataclass
class TableProfile:
    """Observed properties of a delimited table."""

    path: str
    encoding: str
    delimiter: str
    has_header: bool
    rows: int
    columns: list[ColumnProfile]
    ragged_rows: int
    convention: MissingValueConvention
    warnings: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "has_header": self.has_header,
            "rows": self.rows,
            "column_count": len(self.columns),
            "missing_convention": self.convention.as_dict(),
            "columns": [column.as_dict() for column in self.columns],
            "ragged_rows": self.ragged_rows,
            "missing": {column.name: column.missing for column in self.columns},
            "warnings": list(self.warnings),
        }


def profile_table(
    path: Path,
    relative_path: str,
    convention: MissingValueConvention = DEFAULT_CONVENTION,
) -> TableProfile | None:
    """Profile a delimited file, or return ``None`` when it cannot be read as one.

    Returning ``None`` is a legitimate outcome: a file whose delimiter cannot be
    established is not silently forced into a table shape.
    """
    text, encoding, decode_warning = _read_text(path)
    if text is None:
        return None

    warnings: list[str] = []
    if decode_warning:
        warnings.append(decode_warning)

    delimiter, delimiter_warning = _resolve_delimiter(path, text)
    if delimiter is None:
        return None
    if delimiter_warning:
        warnings.append(delimiter_warning)

    reader = csv.reader(text.splitlines(), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return TableProfile(
            relative_path,
            encoding,
            delimiter,
            False,
            0,
            [],
            0,
            convention,
            [*warnings, "file is empty"],
        )

    columns = [
        ColumnProfile(name=_header_name(name, index), position=index)
        for index, name in enumerate(header)
    ]
    if len({column.name for column in columns}) != len(columns):
        warnings.append("header contains duplicate column names; positions disambiguate them")

    rows = 0
    ragged = 0
    for record in reader:
        if not record:
            continue  # A blank line carries no observation.
        rows += 1
        if len(record) != len(columns):
            ragged += 1
        for index, raw in enumerate(record):
            if index >= len(columns):
                continue  # Extra fields are counted via ``ragged``, not invented into columns.
            _observe(columns[index], raw, convention)

    for column in columns:
        # Any cell not seen at all (a short row) is an absent value, not a value.
        column.missing_empty = rows - column.values - column.missing_sentinel
        column.missing = column.missing_empty + column.missing_sentinel
        if not column._overflowed:
            column.distinct = len(column._seen)
            column.distinct_values = sorted(column._seen)

    warnings.extend(_convention_warnings(columns, convention))
    if ragged:
        warnings.append(f"{ragged} row(s) do not have {len(columns)} fields")

    return TableProfile(
        relative_path, encoding, delimiter, True, rows, columns, ragged, convention, warnings
    )


def _observe(column: ColumnProfile, raw: str, convention: MissingValueConvention) -> None:
    value = raw.strip()
    if value == "":
        return  # Empty cell: folded into missing_empty once the row count is known.

    classification = convention.classify(value)
    if classification == SENTINEL:
        column.missing_sentinel += 1
        column.sentinel_tokens_seen[value] = column.sentinel_tokens_seen.get(value, 0) + 1
        return  # A resolved sentinel is missing: it is not a value, and has no type.

    if classification == AMBIGUOUS:
        column.ambiguous_tokens_seen[value] = column.ambiguous_tokens_seen.get(value, 0) + 1
        # Falls through: an unresolved token is still a value the file records.

    column.values += 1
    column.dtype = _promote(column.dtype, _token_type(value))
    if not column._overflowed:
        column._seen.add(value)
        if len(column._seen) > _MAX_ENUMERATED_DISTINCT:
            column._overflowed = True
            column.distinct_exact = False
            column.distinct = len(column._seen)
            column._seen.clear()
    else:
        column.distinct += 1  # Upper bound: see ``distinct_exact``.


def _convention_warnings(
    columns: list[ColumnProfile], convention: MissingValueConvention
) -> list[str]:
    warnings: list[str] = []
    for column in columns:
        if column.sentinel_tokens_seen:
            tokens = ", ".join(sorted(column.sentinel_tokens_seen))
            warnings.append(
                f"column '{column.name}': {column.missing_sentinel} cell(s) holding "
                f"{tokens} resolved to missing by the '{convention.id}' convention "
                f"({convention.source})"
            )
        if column.ambiguous_tokens_seen:
            tokens = ", ".join(sorted(column.ambiguous_tokens_seen))
            total = sum(column.ambiguous_tokens_seen.values())
            warnings.append(
                f"column '{column.name}': {total} cell(s) holding {tokens} were NOT "
                f"resolved to missing; declare a missing-value convention if they should be"
            )
    return warnings


def _token_type(value: str) -> str:
    if _INTEGER.match(value):
        return "integer"
    if _NUMBER.match(value):
        return "number"
    if value.lower() in _BOOLEAN:
        return "boolean"
    return "string"


# Least-upper-bound lattice: empty < integer < number, boolean stands alone, and
# any disagreement collapses to string. No column is ever narrowed by sampling.
_PROMOTIONS = {
    ("empty", "integer"): "integer",
    ("empty", "number"): "number",
    ("empty", "boolean"): "boolean",
    ("empty", "string"): "string",
    ("integer", "number"): "number",
    ("number", "integer"): "number",
}


def _promote(current: str, observed: str) -> str:
    if current == observed:
        return current
    return _PROMOTIONS.get((current, observed), "string")


def _header_name(name: str, index: int) -> str:
    cleaned = name.strip()
    return cleaned if cleaned else f"column_{index + 1}"


def _read_text(path: Path) -> tuple[str | None, str, str | None]:
    text, encoding = read_text(path)
    if text is None:
        return None, "", None
    note = (
        "file begins with a UTF-8 byte-order mark; it was stripped before parsing"
        if encoding == "utf-8-sig"
        else None
    )
    return text, encoding, note


def _resolve_delimiter(path: Path, text: str) -> tuple[str | None, str | None]:
    """Pick a delimiter from the extension, or from consistent field counts."""
    explicit = _EXTENSION_DELIMITERS.get(path.suffix.lower())
    if explicit is not None:
        return explicit, None

    lines = [line for line in text.splitlines()[:_SNIFF_LINES] if line.strip()]
    if not lines:
        return None, None

    for candidate in _CANDIDATE_DELIMITERS:
        counts = {len(row) for row in csv.reader(lines, delimiter=candidate)}
        if len(counts) == 1 and counts.pop() > 1:
            return candidate, f"delimiter inferred as {candidate!r} from consistent field counts"
    return None, None
