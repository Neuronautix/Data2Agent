"""Delimited-table profiling.

This module answers only questions the bytes can answer: how many rows, what the
header says, which cells are empty, and what shape the observed tokens have. It
never decides what a column *means*, and it never decides that a sentinel string
such as ``NA`` represents a missing value -- that is a dataset-specific
convention, so we count such tokens separately and let a human or a downstream
profile rule on them.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

# Delimiters we are willing to consider, in preference order.
_CANDIDATE_DELIMITERS = (",", "\t", ";", "|")
_EXTENSION_DELIMITERS = {".csv": ",", ".tsv": "\t", ".tab": "\t"}
_ENCODINGS = ("utf-8", "utf-8-sig")
_SNIFF_LINES = 20

_INTEGER = re.compile(r"^[+-]?\d+$")
_NUMBER = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")
_BOOLEAN = {"true", "false"}

# Tokens that datasets *often* use for "no value" -- reported, never assumed.
NULL_LIKE_TOKENS = frozenset({"na", "n/a", "nan", "null", "none", "nil", ".", "-", "?", "unknown"})

# Distinct values are only enumerated below this cardinality; above it a column
# is treated as free-form and only its count is kept.
_MAX_ENUMERATED_DISTINCT = 25


@dataclass
class ColumnProfile:
    """Observed, non-interpretive properties of one column."""

    name: str
    position: int
    dtype: str = "empty"
    non_empty: int = 0
    missing: int = 0
    null_like: int = 0
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
            "non_empty": self.non_empty,
            "missing": self.missing,
            "null_like_tokens": self.null_like,
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
    warnings: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "has_header": self.has_header,
            "rows": self.rows,
            "column_count": len(self.columns),
            "columns": [column.as_dict() for column in self.columns],
            "ragged_rows": self.ragged_rows,
            "missing": {column.name: column.missing for column in self.columns},
            "warnings": list(self.warnings),
        }


def profile_table(path: Path, relative_path: str) -> TableProfile | None:
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
            relative_path, encoding, delimiter, False, 0, [], 0, warnings + ["file is empty"]
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
            _observe(columns[index], raw)

    for column in columns:
        column.missing = rows - column.non_empty
        if not column._overflowed:
            column.distinct = len(column._seen)
            column.distinct_values = sorted(column._seen)

    if ragged:
        warnings.append(f"{ragged} row(s) do not have {len(columns)} fields")
    for column in columns:
        if column.null_like:
            warnings.append(
                f"column '{column.name}' contains {column.null_like} null-like token(s) "
                f"(e.g. 'NA'); these are counted separately and NOT treated as missing"
            )

    return TableProfile(relative_path, encoding, delimiter, True, rows, columns, ragged, warnings)


def _observe(column: ColumnProfile, raw: str) -> None:
    value = raw.strip()
    if value == "":
        return  # Empty cell: counted as missing via ``rows - non_empty``.
    column.non_empty += 1
    if value.lower() in NULL_LIKE_TOKENS:
        column.null_like += 1
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
    for encoding in _ENCODINGS:
        try:
            return path.read_text(encoding=encoding), encoding, None
        except UnicodeDecodeError:
            continue
        except OSError:
            return None, "", None
    return None, "", None


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
