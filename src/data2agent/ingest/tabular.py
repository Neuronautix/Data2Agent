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
import io
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import layout as layouts
from . import metadata
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

# Uniqueness needs the whole value set, which ``distinct`` stops tracking at 25.
# It is therefore tracked separately, and only for columns named as a subject
# identifier -- a handful per table -- because keeping every column's values
# would make memory grow with the file. Past this cap the answer becomes "not
# determined", never "not unique": an unbounded scan is the one thing a profiler
# of arbitrarily large files cannot promise.
_MAX_TRACKED_KEYS = 200_000

# Free text is profiled, but its values are not listed (D2A-110).
#
# A value list is what makes a coded column legible without reading it --
# genotype, sex, behaviour, treatment -- and it is copied into the manifest,
# which is the document that travels. A comment column's list is the comments
# themselves: a scorer's notes, a sentence about an animal. So a column whose
# values look like prose keeps every count (distinct, missing, dtype) and loses
# only the list, which stays readable from the verified file through read_rows.
#
# The rule is structural and never reads a column's NAME: a column named
# "Comment" holding one-word codes is a coded column, and a column named "A"
# holding sentences is prose. It is evaluated over the column's distinct values,
# which is every value that could be listed -- above the enumeration cap no list
# is emitted at all, so no verdict is needed.
#
# A *word* is a whitespace-separated token holding at least two letters (in any
# script): 'bad', 'mg/kg' and 'Shank3' are words, '12', '+/-' and a date are
# not. The list is withheld when either clause holds:
#
# * ``prose``: some value holds at least ``_PROSE_WORDS`` words, or is longer
#   than ``_PROSE_LENGTH`` characters -- a sentence, whatever else is true;
# * ``unrepeated-words``: some value holding at least two words occurs only
#   once in the column. A code is a small vocabulary used over and over --
#   "Shank3 Het" on every other row, a behaviour on hundreds of events -- while
#   a note ("bad video") is written once, often among repeated one-word values
#   ("ok"). Repetition of each multi-word value, not of the column on average,
#   is what tells two short words of a code from two short words of a comment;
#   the shape alone cannot.
#
# Calibrated on the local screening datasets (6 datasets, ~9 200 listed
# columns): genotype, sex, treatment and behaviour columns keep their lists,
# comment columns lose theirs. When in doubt the rule withholds -- a small table
# whose two-word codes each occur once is withheld -- because the costs are not
# symmetric: a withheld list costs one read_rows call, a listed comment cannot
# be taken back. A layout declaration can override the verdict per column,
# either way, and the profile records which rule decided and on what.
FREE_TEXT_RULE_ID = "d2a-free-text/1"
_PROSE_WORDS = 4
_PROSE_LENGTH = 40
_LETTER = re.compile(r"[^\W\d_]")


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
    # Whether every value in this column differs from every other. None means
    # not determined -- either the column was not tracked, or it exceeded the
    # tracking cap. Tracked only where ``track_unique`` was asked for.
    unique: bool | None = None
    track_unique: bool = False
    # The header block's own cells for this column, top row first. Set only for
    # a multi-row header, whose composed name would otherwise hide what the
    # file actually says (and which labels were carried across by a fill).
    header_cells: list[str] | None = None
    # A verdict on free text stated rather than observed: by a layout
    # declaration ("declaration"), or by a source format that defines the
    # column as free text by construction ("boris" for a BORIS comment). None
    # leaves the decision to the structural rule.
    free_text: bool | None = None
    free_text_source: str | None = None
    _seen: set[str] = field(default_factory=set, repr=False)
    # Shape of the listed candidates: the most alphabetic words and the most
    # characters in any one distinct value. Bounded: measured only while the
    # value set is still enumerated.
    _max_words: int = field(default=0, repr=False)
    _max_length: int = field(default=0, repr=False)
    # Occurrences of each distinct value holding two or more words; at most
    # _MAX_ENUMERATED_DISTINCT + 1 entries, like the value set itself.
    _multiword: dict[str, int] = field(default_factory=dict, repr=False)
    _overflowed: bool = field(default=False, repr=False)
    _keys_seen: set[str] = field(default_factory=set, repr=False)
    _keys_overflowed: bool = field(default=False, repr=False)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "position": self.position,
            **({"header_cells": list(self.header_cells)} if self.header_cells is not None else {}),
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
            withheld = self.values_withheld()
            if withheld is None:
                payload["distinct_values"] = self.distinct_values
            else:
                # No list, and a reason: "withheld as free text" must never read
                # like "too many distinct values" (distinct_exact: false) or "no
                # values" (distinct: 0).
                payload["values_withheld"] = withheld
            if self.free_text is False and self._structurally_free_text():
                # Listed only because a declaration said so; kept auditable.
                payload["values_listed_by"] = {
                    "rule": "declared",
                    "source": self.free_text_source,
                    "structural_rule": FREE_TEXT_RULE_ID,
                }
        if self.track_unique:
            # Emitted only where uniqueness was actually looked for, so an absent
            # field reads as "not asked" rather than as "asked and found false".
            payload["unique"] = self.unique
        return payload

    def values_withheld(self) -> dict[str, object] | None:
        """Why this column's value list is not in the manifest, or ``None`` if it is."""
        if self.free_text is False:
            return None
        clause = self._free_text_clause()
        if self.free_text is True:
            payload: dict[str, object] = {"reason": "free_text", "rule": "declared"}
            payload["source"] = self.free_text_source
        elif clause is not None:
            payload = {"reason": "free_text", "rule": FREE_TEXT_RULE_ID, "clause": clause}
        else:
            return None
        # The statistics the structural rule reads, recorded whichever rule
        # decided, so a verdict can be checked against the profile.
        payload["max_words"] = self._max_words
        payload["max_length"] = self._max_length
        payload["unrepeated_multiword_values"] = self._unrepeated_multiword()
        return payload

    def _unrepeated_multiword(self) -> int:
        return sum(1 for count in self._multiword.values() if count == 1)

    def _structurally_free_text(self) -> bool:
        return self._free_text_clause() is not None

    def _free_text_clause(self) -> str | None:
        if self._max_words >= _PROSE_WORDS or self._max_length > _PROSE_LENGTH:
            return "prose"
        if self._unrepeated_multiword():
            return "unrepeated-words"
        return None


def new_column(name: str, position: int) -> ColumnProfile:
    """Build a column profile, enabling uniqueness tracking where it is useful.

    The decision is made here rather than at each call site so that a worksheet
    and a CSV observe exactly the same thing -- the metadata rule that consumes
    ``unique`` must not depend on which profiler produced the column.
    """
    return ColumnProfile(
        name=name, position=position, track_unique=metadata.is_subject_identifier_name(name)
    )


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
    layout: layouts.HeaderLayout | None = None
    block: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "has_header": self.has_header,
            **header_fields(self.layout),
            "rows": self.rows,
            "column_count": len(self.columns),
            "missing_convention": self.convention.as_dict(),
            "columns": [column.as_dict() for column in self.columns],
            "ragged_rows": self.ragged_rows,
            "missing": {column.name: column.missing for column in self.columns},
            "warnings": list(self.warnings),
        }
        if self.block is not None:
            # Only on a declared block, so every other table's profile is unchanged.
            payload["block"] = dict(self.block)
        return payload


def header_fields(layout: layouts.HeaderLayout | None) -> dict[str, Any]:
    """The header fields of a table profile; all present, null where unknown.

    Shared by the delimited and the worksheet profile so both carry the same
    keys in the same order, and a reader never has to ask which kind it holds.
    """
    if layout is None:
        return {
            "header_row": None,
            "header_rows": 0,
            "data_starts_row": None,
            "header_source": None,
            "header_detection": None,
        }
    return layout.as_dict()


def numbered_records(reader: Any) -> Iterator[layouts.Row]:
    """csv records numbered by the physical line on which each one *starts*.

    ``csv.reader.line_num`` is the line a record ends on; a quoted cell can span
    lines, so the start is one past the previous record's end. The row reader
    numbers records the same way, which is what makes a recorded
    ``data_starts_row`` mean the same line at ingest and at query time.
    """
    previous_end = 0
    for record in reader:
        yield layouts.Row(previous_end + 1, record)
        previous_end = reader.line_num


def profile_table(
    path: Path,
    relative_path: str,
    convention: MissingValueConvention = DEFAULT_CONVENTION,
    declared: layouts.TableLayout | None = None,
) -> TableProfile | None:
    """Profile a delimited file, or return ``None`` when it cannot be read as one.

    Returning ``None`` is a legitimate outcome: a file whose delimiter cannot be
    established is not silently forced into a table shape.

    The header is decided by :mod:`~data2agent.ingest.layout` -- by ``declared``
    when given, by its rule otherwise -- and the decision is recorded in the
    profile. Leading blank lines are layout, as they are in a worksheet.
    """
    opened = _open_delimited(path)
    if opened is None:
        return None
    text, encoding, delimiter, warnings = opened
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    return _profile_records(
        numbered_records(reader),
        relative_path,
        encoding,
        delimiter,
        convention,
        declared,
        warnings,
    )


def profile_table_blocks(
    path: Path,
    relative_path: str,
    convention: MissingValueConvention,
    declared: layouts.TableBlocks,
) -> list[TableProfile] | None:
    """Profile each declared block of a delimited file as a table of its own (D2A-103).

    Each block reads its own region -- from just below the nearest block above
    it to its ``last_row`` -- sliced to its columns, so the lines between two
    blocks become the lower block's recorded preamble. Column positions stay the
    file's own, so a row reader indexes a record exactly as the profile did.
    """
    opened = _open_delimited(path)
    if opened is None:
        return None
    text, encoding, delimiter, warnings = opened
    profiles: list[TableProfile] = []
    for block in declared.blocks:
        start = declared.region_start(block)
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        seen = layouts.BoundedRows(numbered_records(reader), start, block)
        profile = _profile_records(
            seen,
            f"{relative_path}#{block.name}",
            encoding,
            delimiter,
            convention,
            block.layout,
            list(warnings),
            block=block,
        )
        seen.require_end(f"{relative_path}#{block.name}")
        profile.block = layouts.block_record(block, relative_path, relative_path, start)
        profiles.append(profile)
    return profiles


def _open_delimited(path: Path) -> tuple[str, str, str, list[str]] | None:
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
    # Split with the same newline handling the row reader's file handle uses
    # (newline=""), so a line number means the same line in both places.
    return text, encoding, delimiter, warnings


def _profile_records(
    records: Iterable[layouts.Row],
    relative_path: str,
    encoding: str,
    delimiter: str,
    convention: MissingValueConvention,
    declared: layouts.TableLayout | None,
    warnings: list[str],
    *,
    block: layouts.BlockLayout | None = None,
) -> TableProfile:
    layout, body = layouts.split_header(records, declared)
    warnings.extend(layout.warnings)
    if layout.header_row is None:
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
            layout,
        )

    offset = block.first_position if block is not None else 0
    names, renamed = _unique_names(
        [_header_name(name, offset + index) for index, name in enumerate(layout.labels)]
    )
    columns = [new_column(name, index) for index, name in enumerate(names)]
    if layout.header_cells is not None:
        for column, cells in zip(columns, layout.header_cells, strict=True):
            column.header_cells = cells
    if renamed:
        warnings.append(
            f"header repeats {renamed} column name(s); each repeat is suffixed '.1', '.2', ... "
            f"so that every column stays addressable by name"
        )

    rows = 0
    ragged = 0
    for numbered in body:
        record = numbered.cells
        if not record:
            continue  # A blank line carries no observation.
        if block is not None and not any(str(cell).strip() for cell in record):
            continue  # Blank within the block's columns: nothing observed here either.
        rows += 1
        if len(record) != len(columns):
            ragged += 1
        for index, raw in enumerate(record):
            if index >= len(columns):
                continue  # Extra fields are counted via ``ragged``, not invented into columns.
            _observe(columns[index], raw, convention)

    for column in columns:
        # Any cell not seen at all (a short row) is an absent value, not a value.
        finalise(column, rows)
        # Positions are the file's own, even when a block starts past column A.
        column.position += offset

    warnings.extend(_convention_warnings(columns, convention))
    if ragged:
        warnings.append(f"{ragged} row(s) do not have {len(columns)} fields")

    return TableProfile(
        relative_path,
        encoding,
        delimiter,
        True,
        rows,
        columns,
        ragged,
        convention,
        warnings,
        layout,
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
    if column.track_unique and not column._keys_overflowed:
        column._keys_seen.add(value)
        if len(column._keys_seen) > _MAX_TRACKED_KEYS:
            column._keys_overflowed = True
            column._keys_seen.clear()
    if not column._overflowed:
        if value not in column._seen:
            # Measured once per distinct value, and only while a list is still
            # possible: the free-text verdict concerns exactly the values that
            # would be listed.
            words = sum(1 for token in value.split() if len(_LETTER.findall(token)) >= 2)
            column._max_words = max(column._max_words, words)
            column._max_length = max(column._max_length, len(value))
            if words >= 2:
                column._multiword[value] = 0
        if value in column._multiword:
            column._multiword[value] += 1
        column._seen.add(value)
        if len(column._seen) > _MAX_ENUMERATED_DISTINCT:
            column._overflowed = True
            column.distinct_exact = False
            column.distinct = len(column._seen)
            column._seen.clear()
            column._multiword.clear()
    else:
        column.distinct += 1  # Upper bound: see ``distinct_exact``.


def finalise(column: ColumnProfile, rows: int) -> None:
    """Close a column once the row count is known.

    Shared with the workbook reader so that the invariant
    ``missing == missing_empty + missing_sentinel`` and the uniqueness verdict
    are computed once, in one place, for both kinds of table.
    """
    column.missing_empty = rows - column.values - column.missing_sentinel
    column.missing = column.missing_empty + column.missing_sentinel
    if not column._overflowed:
        column.distinct = len(column._seen)
        column.distinct_values = sorted(column._seen)
    if column.track_unique:
        column.unique = None if column._keys_overflowed else len(column._keys_seen) == column.values


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


def _unique_names(labels: list[str]) -> tuple[list[str], int]:
    """Suffix repeated names '.1', '.2', ... and count how many were renamed.

    Every consumer -- the name-keyed ``missing`` map, the row readers' column
    specs, filters, joins -- addresses a column by name. Two columns sharing a
    name therefore made the earlier one unreachable: a read returned the later
    cell under both. Behavioural-scoring exports repeat metric names across
    behaviours on every column, so this was not an edge case.

    Suffixes are checked against what has been *emitted*, exactly as the
    worksheet profiler does: suffixing against the original labels lets
    ``['id', 'id', 'id.1']`` collapse into two ``'id.1'`` columns.
    """
    emitted: set[str] = set()
    names: list[str] = []
    renamed = 0
    for label in labels:
        name = label
        if name in emitted:
            renamed += 1
            suffix = 1
            while f"{label}.{suffix}" in emitted:
                suffix += 1
            name = f"{label}.{suffix}"
        emitted.add(name)
        names.append(name)
    return names, renamed


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
    """Resolve a delimiter from the bytes, using the extension only as a hint.

    Scientific exports routinely use a '.csv' filename for semicolon-delimited
    content. Treating the suffix as authority collapses such a table to one
    column and corrupts every downstream structural fact. We therefore test the
    extension-implied delimiter against a bounded sample, then prefer another
    supported delimiter only when it gives a stable multi-column parse.

    A genuinely one-column CSV remains valid: when no supported delimiter gives
    a stable multi-column parse, the explicit extension delimiter is retained.
    """
    explicit = _EXTENSION_DELIMITERS.get(path.suffix.lower())
    lines = [line for line in text.splitlines()[:_SNIFF_LINES] if line.strip()]
    if not lines:
        return explicit, None

    plausible: list[tuple[str, int]] = []
    for candidate in _CANDIDATE_DELIMITERS:
        widths = [len(row) for row in csv.reader(lines, delimiter=candidate)]
        if widths and len(set(widths)) == 1 and widths[0] > 1:
            plausible.append((candidate, widths[0]))

    if explicit is not None:
        explicit_hit = next((item for item in plausible if item[0] == explicit), None)
        if explicit_hit is not None:
            return explicit, None
        if plausible:
            # Prefer the candidate exposing the widest consistent structure;
            # candidate order is the deterministic tie-breaker.
            candidate, width = max(plausible, key=lambda item: item[1])
            return (
                candidate,
                f"extension implies delimiter {explicit!r}, but a bounded sample is "
                f"consistently {width} fields with {candidate!r}; reporting the bytes",
            )
        return explicit, None

    if plausible:
        candidate, _ = plausible[0]
        return candidate, f"delimiter inferred as {candidate!r} from consistent field counts"
    return None, None
