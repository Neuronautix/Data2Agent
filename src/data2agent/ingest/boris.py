"""BORIS projects read as tables: events, behaviour intervals, observations.

A BORIS project (see :mod:`~data2agent.ingest.formats`) is the scorer's own
record: every coded event, in the file, before any export. An agent asking
"how long did this scorer record scratching in this observation" needs those
events as rows, not a count of observations -- so each project yields three
tables, keyed ``<file>#events``, ``<file>#intervals`` and ``<file>#observations``,
readable with the same tools as any CSV or worksheet.

Two rules govern everything here.

**One reading, used twice.** :func:`build_tables` is the only code that turns a
project into rows. Ingest profiles its output, and the row reader calls it again
on the checksum-verified file at query time, so a profile and the rows it
describes cannot come from two different interpretations of the same bytes.

**Only verified shapes are read.** BORIS stores an event as a JSON array. The
layouts accepted -- ``[time, subject, behavior, modifiers, comment]`` and the
same plus a frame index -- are the ones observed in real project files (format
version 7.0, 51 388 events, both with and without the sixth field). Any other
shape makes the event and interval tables *unprofiled*, with a warning saying
where. Guessing that a seventh field is an image path, or that a string time is
seconds, would put an unverified reading behind every number derived from it.

What is deliberately not derived: a scorer. BORIS records none per observation
(a scorer's name may be part of an observation id, which is the owner's naming,
not a field), so no scorer column exists rather than one filled by inference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .conventions import MissingValueConvention
from .tabular import ColumnProfile, _convention_warnings, _observe, finalise, new_column

EVENTS = "events"
INTERVALS = "intervals"
OBSERVATIONS = "observations"
TABLES = (EVENTS, INTERVALS, OBSERVATIONS)

# Column names follow BORIS's own tabular exports where BORIS has one ("Observation
# id", "Behavior", "Behavior type", "Start (s)", "Duration (s)", ...), so a scorer
# who knows the exports recognises the columns, and an agent's answer can be
# checked against an export of the same project.
EVENT_COLUMNS = (
    "Observation id",
    "Event index",
    "Time (s)",
    "Subject",
    "Behavior",
    "Behavioral category",
    "Behavior type",
    "Event type",
    "Modifiers",
    "Comment",
    "Frame index",
)
INTERVAL_COLUMNS = (
    "Observation id",
    "Subject",
    "Behavior",
    "Behavioral category",
    "Behavior type",
    "Modifiers",
    "Start (s)",
    "Stop (s)",
    "Duration (s)",
    "Pairing",
    "Start event index",
    "Stop event index",
    "Comment start",
    "Comment stop",
)
OBSERVATION_COLUMNS = (
    "Observation id",
    "Observation date",
    "Description",
    "Observation type",
    "Media files",
    "Media file count",
    "Media duration (s)",
    "Events",
    "First event (s)",
    "Last event (s)",
)
COLUMNS = {EVENTS: EVENT_COLUMNS, INTERVALS: INTERVAL_COLUMNS, OBSERVATIONS: OBSERVATION_COLUMNS}
# Columns holding what a person typed, not a code BORIS defines: the event
# comments and the observation's description. Their values are never listed.
FREE_TEXT_COLUMNS = frozenset({"Comment", "Comment start", "Comment stop", "Description"})
# Columns whose values come from a closed vocabulary by construction: the
# ethogram's behaviour codes and categories, and the three vocabularies this
# module itself writes. A two-word code ("hind scratch") is still a code, so the
# structural rule is not asked. Subjects and modifiers are left to the rule:
# BORIS does not guarantee they come from a defined list.
CODED_COLUMNS = frozenset(
    {"Behavior", "Behavioral category", "Behavior type", "Event type", "Pairing"}
)

# How each table's rows are located, returned with every read.
ROW_LOCATORS = {
    EVENTS: "observation id + 0-based index of the event in that observation's 'events' array",
    INTERVALS: "observation id + 0-based indices of the start and stop events",
    OBSERVATIONS: "observation id",
}

# The 'Pairing' outcomes, in the order they are reported.
PAIRING_OUTCOMES = ("paired", "point", "unmatched_start", "unknown_type")

# Event array layouts verified against real projects; index -> meaning.
_EVENT_LENGTHS = frozenset({5, 6})
_MEDIA_SEPARATOR = " | "


@dataclass
class BorisTable:
    """The rows of one derived table, or the reason it could not be derived."""

    name: str
    columns: tuple[str, ...]
    rows: list[dict[str, Any]] | None  # {"locator": {...}, "values": {column: raw}}
    warnings: list[str] = field(default_factory=list)
    pairing: dict[str, int] | None = None

    @property
    def profiled(self) -> bool:
        return self.rows is not None


def build_tables(document: Any) -> dict[str, BorisTable]:
    """Derive the three tables from a parsed BORIS project. Never raises on content."""
    observations = document.get("observations") if isinstance(document, dict) else None
    if not isinstance(observations, dict) or not all(
        isinstance(item, dict) for item in observations.values()
    ):
        reason = "the project's 'observations' is not an object of observation objects"
        return {name: BorisTable(name, COLUMNS[name], None, [reason]) for name in TABLES}

    ethogram, ethogram_warnings = _ethogram(document.get("behaviors_conf"))
    ordered_ids = sorted(observations)
    shape_error = _event_shape_error(observations, ordered_ids)

    tables = {OBSERVATIONS: _observation_table(observations, ordered_ids, shape_error is None)}
    if shape_error is not None:
        reason = (
            f"{shape_error}; BORIS event layouts read are [time, subject, behavior, "
            f"modifiers, comment] and the same plus a frame index, so this table is not "
            f"derived rather than guessed"
        )
        tables[EVENTS] = BorisTable(EVENTS, EVENT_COLUMNS, None, [reason])
        tables[INTERVALS] = BorisTable(INTERVALS, INTERVAL_COLUMNS, None, [reason])
        return tables

    events, intervals, pairing, unknown = _events_and_intervals(observations, ordered_ids, ethogram)
    notes = list(ethogram_warnings)
    if unknown:
        notes.append(
            f"{unknown} event(s) name a behaviour the ethogram does not type as a state or "
            f"point event; their event type is null and they are not paired"
        )
    tables[EVENTS] = BorisTable(EVENTS, EVENT_COLUMNS, events, list(notes))
    tables[INTERVALS] = BorisTable(INTERVALS, INTERVAL_COLUMNS, intervals, list(notes), pairing)
    return tables


@dataclass
class BorisTableProfile:
    """The manifest's record of one derived table: counts and column profiles only.

    Duck-types the delimited profile's ``path``/``rows``/``columns`` so the
    pipeline records the same row-count, column and missingness claims for it.
    """

    path: str
    file: str
    table: str
    profiled: bool
    rows: int | None
    columns: list[ColumnProfile]
    convention: MissingValueConvention
    warnings: list[str]
    pairing: dict[str, int] | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            # Names the backing file, as a worksheet names its workbook: the key
            # '<file>#events' is not an inventoried path.
            "boris": {"file": self.file, "table": self.table},
            "profiled": self.profiled,
            "rows": self.rows,
            "column_count": len(self.columns),
            "row_locator": ROW_LOCATORS[self.table],
            "missing_convention": self.convention.as_dict(),
            "columns": [column.as_dict() for column in self.columns],
            "missing": {column.name: column.missing for column in self.columns},
            "warnings": list(self.warnings),
        }
        if self.pairing is not None:
            payload["pairing"] = dict(self.pairing)
        return payload


def profile_project(
    document: Any, relative_path: str, convention: MissingValueConvention
) -> list[BorisTableProfile]:
    """Profile the three tables of one parsed project, in a fixed order."""
    profiles: list[BorisTableProfile] = []
    for name, table in build_tables(document).items():
        path = f"{relative_path}#{name}"
        if not table.profiled:
            profiles.append(
                BorisTableProfile(
                    path, relative_path, name, False, None, [], convention, list(table.warnings)
                )
            )
            continue
        columns, notes = profile_columns(table, convention)
        profiles.append(
            BorisTableProfile(
                path,
                relative_path,
                name,
                True,
                len(table.rows or []),
                columns,
                convention,
                [*table.warnings, *notes],
                table.pairing,
            )
        )
    return sorted(profiles, key=lambda profile: profile.path)


def profile_columns(
    table: BorisTable, convention: MissingValueConvention
) -> tuple[list[ColumnProfile], list[str]]:
    """Profile a derived table's columns exactly as a worksheet's are profiled.

    A JSON value becomes the token a worksheet cell would give (``str(value)``,
    null as empty), and is observed by the delimited profiler's own routine, so
    dtype, missingness under the convention and distinct counts mean what they
    mean for every other table.
    """
    columns = [new_column(name, position) for position, name in enumerate(table.columns)]
    for column in columns:
        if column.name in FREE_TEXT_COLUMNS:
            # Free text by construction (D2A-110): BORIS writes whatever the
            # scorer typed here. Declared at the source, so even a comment that
            # happens to be one short word is never listed in the manifest.
            column.free_text = True
            column.free_text_source = "boris"
        elif column.name in CODED_COLUMNS:
            column.free_text = False
            column.free_text_source = "boris"
    rows = table.rows or []
    for row in rows:
        values = row["values"]
        for column in columns:
            _observe(column, _token(values.get(column.name)), convention)
    for column in columns:
        finalise(column, len(rows))
    return columns, _convention_warnings(columns, convention)


def _token(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _ethogram(behaviors: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Behaviour code -> {'type': 'STATE'|'POINT'|None, 'category': str|None}."""
    ethogram: dict[str, dict[str, Any]] = {}
    conflicting: set[str] = set()
    if not isinstance(behaviors, dict):
        return ethogram, ["the project's ethogram ('behaviors_conf') is not an object"]
    for key in sorted(behaviors):
        entry = behaviors[key]
        if not isinstance(entry, dict) or not isinstance(entry.get("code"), str):
            continue
        code = entry["code"]
        category = entry.get("category")
        typed = {
            "type": _behavior_type(entry.get("type")),
            "category": category if isinstance(category, str) else None,
        }
        if code in ethogram and ethogram[code] != typed:
            conflicting.add(code)
        ethogram.setdefault(code, typed)
    warnings: list[str] = []
    for code in conflicting:
        # Two definitions of one code: which applies is not decidable from the
        # file, so neither is used.
        ethogram[code] = {"type": None, "category": None}
    if conflicting:
        warnings.append(
            f"{len(conflicting)} behaviour code(s) are defined more than once with different "
            f"types or categories; their events are left untyped"
        )
    return ethogram, warnings


def _behavior_type(raw: Any) -> str | None:
    """BORIS's ethogram type ('State event', 'Point event', ... with coding map)."""
    if not isinstance(raw, str):
        return None
    lowered = raw.strip().lower()
    if lowered.startswith("state"):
        return "STATE"
    if lowered.startswith("point"):
        return "POINT"
    return None


def _event_shape_error(observations: dict[str, Any], ordered_ids: list[str]) -> str | None:
    """Where the first event of an unverified shape is, or ``None`` if all are read.

    Located by position (the n-th observation in id order, the event's index),
    never by quoting the observation id or any event value.
    """
    for number, observation_id in enumerate(ordered_ids, start=1):
        events = observations[observation_id].get("events")
        if events is None:
            continue  # an observation not yet scored holds no events
        if not isinstance(events, list):
            return f"observation {number} (in id order) stores 'events' as something not a list"
        for index, event in enumerate(events):
            if not _event_is_read(event):
                return f"event {index} of observation {number} (in id order) has an unread shape"
    return None


def _event_is_read(event: Any) -> bool:
    if not isinstance(event, list) or len(event) not in _EVENT_LENGTHS:
        return False
    time = event[0]
    if isinstance(time, bool) or not isinstance(time, (int, float)) or not math.isfinite(time):
        return False
    if not all(isinstance(item, str) for item in event[1:5]):
        return False
    return len(event) == 5 or (not isinstance(event[5], bool) and isinstance(event[5], (int, str)))


def _events_and_intervals(
    observations: dict[str, Any],
    ordered_ids: list[str],
    ethogram: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int], int]:
    """Every event as a row, and every behaviour interval BORIS's pairing rule yields.

    Pairing is BORIS's own: a state behaviour toggles, so for one subject,
    behaviour and modifier set, occurrences taken in time order alternate
    start, stop, start, stop. Events are ordered by time, ties kept in the
    order the file stores them -- the file's order is not always time order.
    A point event is an interval of length zero. An odd occurrence left open at
    the end of the observation is an ``unmatched_start`` row with a null stop
    and duration: closing it at the media's end, or dropping it, would each
    assert something the scorer did not record. Parity cannot produce an
    unmatched *stop* -- under the toggle rule an unpaired occurrence is always
    the last, and is always a start -- so none is ever reported; a scorer who
    began coding mid-behaviour shows up as that toggle, not as a flag.
    """
    events_rows: list[dict[str, Any]] = []
    interval_rows: list[dict[str, Any]] = []
    pairing = dict.fromkeys(PAIRING_OUTCOMES, 0)
    unknown = 0

    for observation_id in ordered_ids:
        events = observations[observation_id].get("events") or []
        ordered = sorted(range(len(events)), key=lambda index: (events[index][0], index))
        event_types: dict[int, str | None] = {}
        open_starts: dict[tuple[str, str, str], int] = {}
        intervals: list[dict[str, Any]] = []

        for index in ordered:
            event = events[index]
            subject, behavior, modifiers = event[1], event[2], event[3]
            kind = ethogram.get(behavior, {}).get("type")
            if kind == "POINT":
                event_types[index] = "POINT"
                intervals.append(_interval(observation_id, events, ethogram, index, index, "point"))
            elif kind == "STATE":
                key = (subject, behavior, modifiers)
                if key in open_starts:
                    start = open_starts.pop(key)
                    event_types[index] = "STOP"
                    intervals.append(
                        _interval(observation_id, events, ethogram, start, index, "paired")
                    )
                else:
                    open_starts[key] = index
                    event_types[index] = "START"
            else:
                unknown += 1
                event_types[index] = None
                intervals.append(
                    _interval(observation_id, events, ethogram, index, None, "unknown_type")
                )
        for start in open_starts.values():
            intervals.append(
                _interval(observation_id, events, ethogram, start, None, "unmatched_start")
            )

        for index, event in enumerate(events):
            behavior = event[2]
            events_rows.append(
                {
                    "locator": {"observation_id": observation_id, "event_index": index},
                    "values": {
                        "Observation id": observation_id,
                        "Event index": index,
                        "Time (s)": event[0],
                        "Subject": event[1],
                        "Behavior": behavior,
                        "Behavioral category": ethogram.get(behavior, {}).get("category"),
                        "Behavior type": ethogram.get(behavior, {}).get("type"),
                        "Event type": event_types[index],
                        "Modifiers": event[3],
                        "Comment": event[4],
                        "Frame index": event[5] if len(event) > 5 else None,
                    },
                }
            )
        intervals.sort(key=lambda row: (row["values"]["Start (s)"], row["_start"]))
        for row in intervals:
            pairing[row["values"]["Pairing"]] += 1
            del row["_start"]
            interval_rows.append(row)
    return events_rows, interval_rows, pairing, unknown


def _interval(
    observation_id: str,
    events: list[list[Any]],
    ethogram: dict[str, dict[str, Any]],
    start: int,
    stop: int | None,
    outcome: str,
) -> dict[str, Any]:
    first = events[start]
    last = events[stop] if stop is not None else None
    behavior = first[2]
    start_time = first[0]
    stop_time = last[0] if last is not None else None
    return {
        "_start": start,
        "locator": {
            "observation_id": observation_id,
            "start_event_index": start,
            "stop_event_index": stop,
        },
        "values": {
            "Observation id": observation_id,
            "Subject": first[1],
            "Behavior": behavior,
            "Behavioral category": ethogram.get(behavior, {}).get("category"),
            "Behavior type": ethogram.get(behavior, {}).get("type"),
            "Modifiers": first[3],
            "Start (s)": start_time,
            "Stop (s)": stop_time,
            "Duration (s)": None if stop_time is None else _difference(stop_time, start_time),
            "Pairing": outcome,
            "Start event index": start,
            "Stop event index": stop,
            "Comment start": first[4],
            "Comment stop": last[4] if last is not None else None,
        },
    }


def _difference(stop: float, start: float) -> int | float:
    """``stop - start`` in decimal, so 12.3 - 10.1 is 2.2 and not 2.1999999999999993.

    BORIS writes times as the decimal numbers the scorer's clicks produced; a
    binary subtraction would add representation noise that no rounding rule
    could remove without choosing a precision the file never stated.
    """
    difference = Decimal(repr(stop)) - Decimal(repr(start))
    if isinstance(stop, int) and isinstance(start, int):
        return int(difference)
    return float(difference)


def _observation_table(
    observations: dict[str, Any], ordered_ids: list[str], events_read: bool
) -> BorisTable:
    rows: list[dict[str, Any]] = []
    for observation_id in ordered_ids:
        observation = observations[observation_id]
        events = observation.get("events")
        times = [event[0] for event in events] if events_read and isinstance(events, list) else []
        files = _media_files(observation.get("file"))
        rows.append(
            {
                "locator": {"observation_id": observation_id},
                "values": {
                    "Observation id": observation_id,
                    "Observation date": _text(observation.get("date")),
                    "Description": _text(observation.get("description")),
                    "Observation type": _text(observation.get("type")),
                    "Media files": _MEDIA_SEPARATOR.join(name for _, name in files) or None,
                    "Media file count": len(files),
                    "Media duration (s)": _media_duration(observation, files),
                    "Events": len(events) if isinstance(events, list) else None,
                    "First event (s)": min(times) if times else None,
                    "Last event (s)": max(times) if times else None,
                },
            }
        )
    return BorisTable(OBSERVATIONS, OBSERVATION_COLUMNS, rows)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _media_files(raw: Any) -> list[tuple[str, str]]:
    """(player, file name) pairs, in player order; the name only, never its directory.

    BORIS stores each media file's absolute path on the scorer's machine. The
    directory says where that machine kept its videos and nothing about the
    observation, so only the final component is kept.
    """
    files: list[tuple[str, str]] = []
    if not isinstance(raw, dict):
        return files
    for player in sorted(raw, key=lambda key: (len(key), key)):
        paths = raw[player]
        if not isinstance(paths, list):
            continue
        for path in paths:
            if isinstance(path, str) and path.strip():
                files.append((player, path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]))
    return files


def _media_duration(observation: dict[str, Any], files: list[tuple[str, str]]) -> float | None:
    """Summed length of player 1's media, as BORIS's media_info records it.

    Player 1's files play one after another, so their lengths add; other players
    show the same time span from another camera, so they are not added. Null
    when any of player 1's files has no recorded numeric length: a partial sum
    would read as the whole observation's length.
    """
    raw_files = observation.get("file")
    info = observation.get("media_info")
    if not isinstance(raw_files, dict) or not isinstance(info, dict):
        return None
    lengths = info.get("length")
    first_player = raw_files.get("1")
    if not isinstance(lengths, dict) or not isinstance(first_player, list) or not first_player:
        return None
    total = Decimal(0)
    for path in first_player:
        length = lengths.get(path) if isinstance(path, str) else None
        if isinstance(length, bool) or not isinstance(length, (int, float)):
            return None
        if not math.isfinite(length):
            return None
        total += Decimal(repr(length))
    return float(total)
