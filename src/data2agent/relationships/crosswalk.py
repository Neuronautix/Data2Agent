"""Declared identifier crosswalks: explicit written-form -> canonical-ID maps.

Real lab data writes one animal differently in different files: a registry
writes ``X-1_I`` where a measurement sheet writes ``X1-I``; one file has a digit
zero where another has a letter O. Exact-equality joins cannot connect them, and
an *implicit* normalisation (strip dashes, fold case, treat 0 as O) must never be
the answer, because the same rule that connects two spellings of one animal can
just as easily merge two different animals. A single wrong merge is invisible
downstream: the joined rows look exactly like correct ones.

So identity across written forms is only ever *declared*, by a crosswalk file a
person wrote and can cite. This module parses and validates that file and
resolves key values through it. It infers nothing:

* a value is mapped only when it is listed, byte for byte, as a ``form``;
* a value that is not listed is passed through unchanged and counted, never
  guessed -- and it stays in its own namespace, so an unlisted raw value that
  happens to equal a canonical ID does not silently join that canonical ID's
  forms (the coincidence is reported instead);
* a canonical ID is not implicitly one of its own forms; if a table writes the
  canonical spelling, the crosswalk lists it as a form like any other.

A composite key (for example cage and tail in two columns) can be compared with
a single-column written form only through a declared ``key_format`` template
such as ``"{cage}-{tail}"``. The template is rendering, not normalisation: it
concatenates the declared key columns' values exactly as read, and every key
column must appear in it so no part of the key is silently dropped.

Everything here is stdlib-only (``csv``, ``hashlib``) and deterministic.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

CROSSWALK_FORMAT = "data2agent-crosswalk-csv/1"
CANONICAL_COLUMN = "canonical_id"
FORM_COLUMN = "form"
OPTIONAL_COLUMNS = ("source", "note")
_MAX_EXAMPLES = 10
_BOM = "﻿"

# Tags that keep mapped and unmapped key values in separate namespaces. A join
# key is ("canonical", id) when the crosswalk listed the written form and
# ("unmapped", text) when it did not; the two can never compare equal.
MAPPED = "canonical"
UNMAPPED = "unmapped"


class CrosswalkError(ValueError):
    """A crosswalk file or declaration that cannot be used as written."""


@dataclass(frozen=True)
class Crosswalk:
    """One validated crosswalk: which written forms denote which canonical ID."""

    name: str
    sha256: str
    content: str
    forms: dict[str, str]
    rows: tuple[dict[str, str], ...]
    columns: tuple[str, ...]
    # Where the file was read from, so the service can re-hash it later. Not
    # part of the crosswalk's identity: the same bytes elsewhere are the same map.
    source_path: str | None = field(default=None, compare=False)

    @property
    def canonical_ids(self) -> frozenset[str]:
        return frozenset(self.forms.values())

    def lookup(self, text: str) -> str | None:
        return self.forms.get(text)

    def forms_of(self, canonical_id: str) -> list[str]:
        return sorted(form for form, target in self.forms.items() if target == canonical_id)

    def citation(self) -> dict[str, str]:
        return {"name": self.name, "sha256": self.sha256}

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "format": CROSSWALK_FORMAT,
            "columns": list(self.columns),
            "form_count": len(self.forms),
            "canonical_id_count": len(self.canonical_ids),
        }

    def as_record(self) -> dict[str, Any]:
        """The bundle record: the verbatim text plus its hash, nothing re-derived.

        Storing the verbatim text (not a re-serialisation) lets the service
        re-check ``sha256(content) == sha256`` at load time, so an edited entry
        inside relationships.json is caught with the same hash that cites the
        user's file.
        """
        record = {**self.summary(), "content": self.content}
        if self.source_path is not None:
            record["source_path"] = self.source_path
        return record


def load_crosswalk_file(path: Path, name: str | None = None) -> Crosswalk:
    """Read and validate a crosswalk CSV; the name defaults to the file name."""
    resolved = Path(path).expanduser().resolve()
    data = resolved.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CrosswalkError(
            f"crosswalk '{path}' is not UTF-8 ({error}); save it as UTF-8 CSV"
        ) from error
    crosswalk = parse_crosswalk(text, name=name or resolved.name)
    return replace(crosswalk, source_path=str(resolved))


def parse_crosswalk(content: str, *, name: str) -> Crosswalk:
    """Validate crosswalk text strictly and report every problem at once.

    Rejected, with the offending line numbers: a missing header column, an
    unknown column, an empty canonical_id or form, a form listed twice, and --
    the case that matters most -- a form mapped to two different canonical IDs,
    which would make the crosswalk itself ambiguous about which animal a value
    denotes. Values are kept verbatim: no stripping, no case folding.
    """
    if not name or not name.strip():
        raise CrosswalkError("a crosswalk needs a non-empty name")
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    body = content[1:] if content.startswith(_BOM) else content
    reader = csv.reader(io.StringIO(body, newline=""))
    try:
        header = next(reader)
    except StopIteration as error:
        raise CrosswalkError(f"crosswalk '{name}' is empty") from error

    problems: list[str] = []
    if len(set(header)) != len(header):
        problems.append(f"header repeats a column: {header}")
    missing = [column for column in (CANONICAL_COLUMN, FORM_COLUMN) if column not in header]
    if missing:
        problems.append(
            f"header lacks required column(s) {missing}; expected "
            f"'{CANONICAL_COLUMN},{FORM_COLUMN}' plus optional {list(OPTIONAL_COLUMNS)}"
        )
    unknown = [
        column
        for column in header
        if column not in (CANONICAL_COLUMN, FORM_COLUMN, *OPTIONAL_COLUMNS)
    ]
    if unknown:
        problems.append(
            f"header has unknown column(s) {unknown}; allowed: "
            f"{[CANONICAL_COLUMN, FORM_COLUMN, *OPTIONAL_COLUMNS]}"
        )
    if problems:
        raise CrosswalkError(f"crosswalk '{name}' is invalid: " + "; ".join(problems))

    rows: list[dict[str, str]] = []
    first_line: dict[str, int] = {}
    targets: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for record in reader:
        line = reader.line_num
        if not record or all(cell == "" for cell in record):
            continue
        if len(record) != len(header):
            problems.append(
                f"line {line}: {len(record)} field(s) where the header has {len(header)}"
            )
            continue
        row = dict(zip(header, record, strict=True))
        canonical = row[CANONICAL_COLUMN]
        form = row[FORM_COLUMN]
        empty = [column for column in (CANONICAL_COLUMN, FORM_COLUMN) if not row[column].strip()]
        if empty:
            problems.append(f"line {line}: empty {' and '.join(empty)}")
            continue
        targets[form][canonical].append(line)
        if form in first_line:
            continue
        first_line[form] = line
        rows.append(row)

    forms: dict[str, str] = {}
    for form, by_target in targets.items():
        if len(by_target) > 1:
            detail = ", ".join(
                f"{canonical!r} (line(s) {lines})" for canonical, lines in sorted(by_target.items())
            )
            problems.append(f"form {form!r} maps to more than one canonical_id: {detail}")
            continue
        ((canonical, lines),) = by_target.items()
        if len(lines) > 1:
            problems.append(f"form {form!r} is listed more than once (lines {lines})")
            continue
        forms[form] = canonical
    if problems:
        raise CrosswalkError(f"crosswalk '{name}' is invalid: " + "; ".join(problems))
    if not forms:
        raise CrosswalkError(f"crosswalk '{name}' lists no forms")
    return Crosswalk(
        name=name,
        sha256=sha256,
        content=content,
        forms=forms,
        rows=tuple(rows),
        columns=tuple(header),
    )


def crosswalk_from_record(record: dict[str, Any]) -> Crosswalk:
    """Rebuild a crosswalk from its bundle record, refusing a tampered copy."""
    content = record.get("content")
    name = record.get("name")
    if not isinstance(content, str) or not isinstance(name, str):
        raise CrosswalkError("crosswalk record lacks its verbatim 'content' or 'name'")
    crosswalk = parse_crosswalk(content, name=name)
    if crosswalk.sha256 != record.get("sha256"):
        raise CrosswalkError(
            f"crosswalk '{name}' content no longer hashes to its recorded sha256; "
            "the bundle was edited after it was built"
        )
    return replace(crosswalk, source_path=record.get("source_path"))


def render_text(value: Any) -> str:
    """The text a key value is looked up by: strings verbatim, others via str().

    No stripping or case folding: the crosswalk lists forms exactly as the
    reader surfaces them, so a value read as the integer 7 is looked up as "7"
    and one read as the float 7.0 as "7.0".
    """
    return value if isinstance(value, str) else str(value)


@dataclass(frozen=True)
class KeyFormat:
    """A declared template that renders several key columns as one string."""

    template: str
    parts: tuple[tuple[str, str], ...]  # ("literal", text) or ("column", name)

    def render(self, values: dict[str, Any]) -> str:
        return "".join(
            text if kind == "literal" else render_text(values[text]) for kind, text in self.parts
        )

    @property
    def adjacent_placeholders(self) -> list[tuple[str, str]]:
        """Placeholder pairs with no literal between them, e.g. ``{cage}{tail}``.

        Such a template is not injective: ("1", "23") and ("12", "3") both
        render "123". It is not refused outright -- the data may never hit the
        ambiguity, and rendering collisions are detected on the actual values --
        but the declaration is warned that the separator is missing.
        """
        return [
            (first[1], second[1])
            for first, second in zip(self.parts, self.parts[1:], strict=False)
            if first[0] == "column" and second[0] == "column"
        ]


def parse_key_format(template: str, keys: list[str], *, side: str) -> KeyFormat:
    """Parse ``{column}`` placeholders; ``{{`` and ``}}`` are literal braces.

    Every declared key column must be used, and only declared key columns may
    be used: a template that drops a key column would silently widen the match,
    and one that reads an undeclared column would join on something nobody
    declared as a key.
    """
    if not isinstance(template, str) or not template:
        raise CrosswalkError(f"{side}_key_format must be a non-empty string")
    parts: list[tuple[str, str]] = []
    literal: list[str] = []
    index = 0
    while index < len(template):
        char = template[index]
        if char == "{" and template.startswith("{{", index):
            literal.append("{")
            index += 2
        elif char == "}" and template.startswith("}}", index):
            literal.append("}")
            index += 2
        elif char == "{":
            end = template.find("}", index + 1)
            if end == -1:
                raise CrosswalkError(f"{side}_key_format {template!r} has an unclosed '{{'")
            column = template[index + 1 : end]
            if not column or "{" in column:
                raise CrosswalkError(f"{side}_key_format {template!r} has an empty placeholder")
            if literal:
                parts.append(("literal", "".join(literal)))
                literal = []
            parts.append(("column", column))
            index = end + 1
        elif char == "}":
            raise CrosswalkError(f"{side}_key_format {template!r} has an unmatched '}}'")
        else:
            literal.append(char)
            index += 1
    if literal:
        parts.append(("literal", "".join(literal)))
    used = [text for kind, text in parts if kind == "column"]
    undeclared = sorted(set(used) - set(keys))
    unused = [key for key in keys if key not in used]
    if undeclared:
        raise CrosswalkError(
            f"{side}_key_format uses column(s) {undeclared} that are not in {side}_keys {keys}"
        )
    if unused:
        raise CrosswalkError(
            f"{side}_key_format does not use {side}_keys column(s) {unused}; every key "
            "column must appear so no part of the key is silently dropped"
        )
    return KeyFormat(template=template, parts=tuple(parts))


@dataclass
class KeyResolver:
    """Turns one side's key columns into the value the join compares.

    Without a format or crosswalk the join key is the raw value tuple, exactly
    as before crosswalks existed. With a format, it is the rendered string. With
    a crosswalk, it is a tagged ("canonical", id) or ("unmapped", text) pair.
    ``as_text`` renders a plain single column as text: it is set on the side
    facing a rendered key_format, so a value read as the integer 7 is compared
    with a rendered "7" rather than silently failing to match it.
    """

    keys: list[str]
    key_format: KeyFormat | None = None
    crosswalk: Crosswalk | None = None
    side: str = "left"
    as_text: bool = False

    def __post_init__(self) -> None:
        # Checked first: every message below names key columns, and an empty
        # key list must fail as the validation error it is, not an IndexError.
        if not self.keys:
            raise CrosswalkError(f"{self.side}_keys must be non-empty")
        if self.as_text and self.key_format is None and len(self.keys) != 1:
            raise CrosswalkError(
                f"{self.side}_keys has {len(self.keys)} columns but the other side renders "
                f"one value; declare {self.side}_key_format to render this side too"
            )
        if self.crosswalk is not None and self.key_format is None and len(self.keys) != 1:
            raise CrosswalkError(
                f"a crosswalk maps single written forms, but {self.side}_keys has "
                f"{len(self.keys)} columns; declare {self.side}_key_format (for example "
                f"'{{{self.keys[0]}}}-{{{self.keys[-1]}}}') to render the composite key"
            )

    @property
    def transforms(self) -> bool:
        return self.key_format is not None or self.crosswalk is not None or self.as_text

    @property
    def width(self) -> int:
        """How many values the compared key holds: one once rendered or mapped."""
        return 1 if self.transforms else len(self.keys)

    def raw(self, values: dict[str, Any]) -> tuple[Any, ...] | None:
        raw = tuple(values.get(column) for column in self.keys)
        return None if any(value is None for value in raw) else raw

    def text(self, values: dict[str, Any]) -> str:
        if self.key_format is not None:
            return self.key_format.render(values)
        return render_text(values[self.keys[0]])

    def resolve(self, values: dict[str, Any]) -> tuple[Any, ...] | None:
        """The comparable join key, or None when any key column is missing."""
        raw = self.raw(values)
        if raw is None:
            return None
        if not self.transforms:
            return raw
        text = self.text(values)
        if self.crosswalk is None:
            return (text,)
        canonical = self.crosswalk.lookup(text)
        if canonical is None:
            return (UNMAPPED, text)
        return (MAPPED, canonical)

    def describe(self) -> dict[str, Any]:
        described: dict[str, Any] = {"keys": list(self.keys)}
        if self.key_format is not None:
            described["key_format"] = self.key_format.template
        return described


def key_label(key: tuple[Any, ...], resolver: KeyResolver) -> dict[str, Any]:
    """Human/agent-facing form of a join key, never exposing the internal tags."""
    if resolver.crosswalk is not None:
        tag, text = key
        return {
            "key": [text],
            "canonical_id": text if tag == MAPPED else None,
            "mapping": "crosswalk" if tag == MAPPED else "unmapped",
        }
    return {"key": list(key)}


def mapping_facts(
    rows: list[dict[str, Any]],
    resolver: KeyResolver,
    *,
    other_keys: set[tuple[Any, ...]],
) -> dict[str, Any]:
    """Count mapped/unmapped/unmatched values and detect collisions on one side.

    A collision is two *different* written forms in the same table that the
    crosswalk maps to one canonical ID. It is reported, never merged: if the
    crosswalk is right, the table holds one animal under two spellings; if it
    is wrong, it would merge two animals. Only a person can say which.

    A rendering collision comes earlier, before any crosswalk lookup: two
    *different* raw keys that render to the same text -- ("1", "23") and
    ("12", "3") under ``{a}{b}``, or the integer 7 and the string "7". The
    written forms are then identical, so the crosswalk check above cannot see
    it; it is detected here by tracking which raw tuples produced each text.
    """
    raw_by_text: dict[str, dict[tuple[Any, ...], list[Any]]] = defaultdict(
        lambda: defaultdict(list)
    )
    mapped_rows = 0
    unmapped_rows = 0
    forms_by_canonical: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    unmapped_values: dict[str, list[Any]] = defaultdict(list)
    keys_seen: set[tuple[Any, ...]] = set()
    for row in rows:
        key = resolver.resolve(row["values"])
        if key is None:
            continue
        keys_seen.add(key)
        text = resolver.text(row["values"])
        raw = resolver.raw(row["values"])
        assert raw is not None
        raw_by_text[text][raw].append(row.get("source_row"))
        if resolver.crosswalk is None:
            continue
        tag, value = key
        if tag == MAPPED:
            mapped_rows += 1
            forms_by_canonical[value][text].append(row.get("source_row"))
        else:
            unmapped_rows += 1
            unmapped_values[value].append(row.get("source_row"))

    unmatched = sorted(keys_seen - other_keys, key=_sort_key)
    facts: dict[str, Any] = {
        **resolver.describe(),
        "unmatched_distinct_keys": len(unmatched),
        "unmatched_examples": [key_label(key, resolver) for key in unmatched[:_MAX_EXAMPLES]],
        "rendering_collisions": [
            {
                "rendered": text,
                "raw_keys": [
                    {"raw": list(raw), "source_rows": source_rows[:_MAX_EXAMPLES]}
                    for raw, source_rows in sorted(
                        by_raw.items(), key=lambda item: _sort_key(item[0])
                    )
                ],
            }
            for text, by_raw in sorted(raw_by_text.items())
            if len(by_raw) > 1
        ],
    }
    if resolver.crosswalk is None:
        return facts

    collisions = [
        {
            "canonical_id": canonical,
            "forms": [
                {"form": form, "source_rows": source_rows[:_MAX_EXAMPLES]}
                for form, source_rows in sorted(by_form.items())
            ],
        }
        for canonical, by_form in sorted(forms_by_canonical.items())
        if len(by_form) > 1
    ]
    canonical_ids = resolver.crosswalk.canonical_ids
    shadowing = sorted(value for value in unmapped_values if value in canonical_ids)
    facts.update(
        {
            "mapped_rows": mapped_rows,
            "unmapped_rows": unmapped_rows,
            "mapped_distinct_values": sum(len(by_form) for by_form in forms_by_canonical.values()),
            "mapped_distinct_canonical_ids": len(forms_by_canonical),
            "unmapped_distinct_values": len(unmapped_values),
            "unmapped_examples": [
                {"value": value, "source_rows": unmapped_values[value][:_MAX_EXAMPLES]}
                for value in sorted(unmapped_values)[:_MAX_EXAMPLES]
            ],
            "unmapped_values_equal_to_a_canonical_id": shadowing[:_MAX_EXAMPLES],
            "collisions": collisions,
        }
    )
    return facts


def _sort_key(key: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(f"{type(value).__name__}:{value!s}" for value in key)
