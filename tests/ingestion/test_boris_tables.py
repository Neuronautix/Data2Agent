"""BORIS projects as queryable tables: events, intervals, observations (D2A-109).

Every project here is synthetic: invented codes, times and ids, shaped like the
layouts BORIS writes. No real project is used or needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data2agent.ingest import boris, ingest
from data2agent.mcp import DatasetService

PROJECT = "scoring.boris"


def _project(observations: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "project_format_version": "7.0",
        "project_name": "synthetic",
        "behaviors_conf": {
            "0": {"code": "groom", "type": "State event", "category": "care"},
            "1": {"code": "rear", "type": "Point event", "category": ""},
        },
        "subjects_conf": {},
        "observations": observations
        if observations is not None
        else {
            # Stored out of time order, as real projects sometimes are; the
            # first groom is at t=0, and the last groom is never closed.
            "obs-b": {
                "type": "MEDIA",
                "date": "2020-01-02T09:00:00",
                "description": "",
                "file": {"1": ["C:/videos/cam/clip-1.mp4", "D:\\other\\clip-2.mp4"], "2": []},
                "media_info": {
                    "length": {"C:/videos/cam/clip-1.mp4": 30.5, "D:\\other\\clip-2.mp4": 29.5}
                },
                "events": [
                    [0.0, "", "groom", "", "", 0],
                    [4.25, "", "groom", "", "", 127],
                    [2.0, "", "rear", "", "a note", 60],
                    [10.1, "", "groom", "", "", 303],
                    [12.3, "", "groom", "", "", 369],
                    [20.0, "", "groom", "", "", 600],
                ],
            },
            "obs-a": {
                "type": "MEDIA",
                "date": "2020-01-01T09:00:00",
                "events": [
                    [1.5, "", "groom", "", ""],
                    [3.0, "", "groom", "", ""],
                ],
            },
            "obs-empty": {"type": "LIVE", "events": []},
        },
    }


def _ingest(tmp_path: Path, document: object, name: str = "out") -> tuple[dict, DatasetService]:
    source = tmp_path / "ds"
    source.mkdir(exist_ok=True)
    (source / PROJECT).write_text(json.dumps(document), encoding="utf-8")
    result = ingest(source, tmp_path / name)
    return result.manifest, DatasetService(result.output_dir)


def _rows(service: DatasetService, table: str) -> list[dict]:
    return service.read_rows(f"{PROJECT}#{table}", limit=1000)["rows"]


def test_a_project_yields_three_tables(tmp_path: Path):
    manifest, service = _ingest(tmp_path, _project())
    paths = {f"{PROJECT}#{name}" for name in boris.TABLES}
    assert paths <= set(manifest["tables"])
    listed = {t["path"]: t for t in service.list_tables()["tables"]}
    assert listed[f"{PROJECT}#events"]["kind"] == "boris-events"
    assert listed[f"{PROJECT}#intervals"]["backing_file"] == PROJECT
    assert manifest["tables"][f"{PROJECT}#events"]["rows"] == 8
    assert manifest["tables"][f"{PROJECT}#observations"]["rows"] == 3


def test_event_rows_keep_file_order_and_locate_each_event(tmp_path: Path):
    _, service = _ingest(tmp_path, _project())
    rows = [row for row in _rows(service, "events") if row["values"]["Observation id"] == "obs-b"]
    assert [row["source_row"] for row in rows] == [
        {"observation_id": "obs-b", "event_index": index} for index in range(6)
    ]
    first, rear = rows[0]["values"], rows[2]["values"]
    assert first["Time (s)"] == 0.0 and first["Behavior type"] == "STATE"
    assert first["Frame index"] == 0
    assert first["Subject"] is None, "an empty subject is missing, not a name"
    assert rows[0]["missing"]["Subject"] == {"kind": "empty"}
    assert rear["Behavior type"] == "POINT" and rear["Event type"] == "POINT"
    assert rear["Comment"] == "a note"
    assert rear["Behavioral category"] is None
    # BORIS's toggle, in time order: 0.0 start, 4.25 stop, 10.1 start, 12.3 stop, 20.0 start.
    assert [row["values"]["Event type"] for row in rows] == [
        "START", "STOP", "POINT", "START", "STOP", "START",
    ]  # fmt: skip


def test_state_events_pair_in_time_order_and_an_open_start_is_flagged(tmp_path: Path):
    manifest, service = _ingest(tmp_path, _project())
    rows = [r for r in _rows(service, "intervals") if r["values"]["Observation id"] == "obs-b"]
    summary = [
        (
            r["values"]["Behavior"],
            r["values"]["Start (s)"],
            r["values"]["Stop (s)"],
            r["values"]["Duration (s)"],
            r["values"]["Pairing"],
        )
        for r in rows
    ]
    assert summary == [
        # An event at t=0 is a start under BORIS's rule, not an unmatched stop.
        ("groom", 0.0, 4.25, 4.25, "paired"),
        ("rear", 2.0, 2.0, 0.0, "point"),
        ("groom", 10.1, 12.3, 2.2, "paired"),  # decimal, not 2.1999999999999993
        ("groom", 20.0, None, None, "unmatched_start"),
    ]
    unmatched = rows[-1]
    assert unmatched["source_row"] == {
        "observation_id": "obs-b",
        "start_event_index": 5,
        "stop_event_index": None,
    }
    assert unmatched["missing"]["Duration (s)"] == {"kind": "empty"}
    assert manifest["tables"][f"{PROJECT}#intervals"]["pairing"] == {
        "paired": 3,
        "point": 1,
        "unmatched_start": 1,
        "unknown_type": 0,
    }


def test_pairing_is_per_subject_behaviour_and_modifiers():
    events = [
        [1.0, "mouse-1", "groom", "", ""],
        [2.0, "mouse-2", "groom", "", ""],
        [3.0, "mouse-1", "groom", "", ""],
        [4.0, "mouse-2", "groom", "left", ""],
    ]
    tables = boris.build_tables(_project({"o": {"events": events}}))
    pairs = [
        (row["values"]["Subject"], row["values"]["Pairing"], row["values"]["Duration (s)"])
        for row in tables[boris.INTERVALS].rows or []
    ]
    assert pairs == [
        ("mouse-1", "paired", 2.0),
        ("mouse-2", "unmatched_start", None),
        ("mouse-2", "unmatched_start", None),
    ]


def test_a_behaviour_missing_from_the_ethogram_is_not_paired():
    events = [[1.0, "", "sniff", "", ""], [2.0, "", "sniff", "", ""]]
    tables = boris.build_tables(_project({"o": {"events": events}}))
    intervals = tables[boris.INTERVALS]
    assert [row["values"]["Pairing"] for row in intervals.rows or []] == ["unknown_type"] * 2
    assert [row["values"]["Event type"] for row in tables[boris.EVENTS].rows or []] == [None] * 2
    assert any("ethogram" in note for note in intervals.warnings)


def test_the_observations_table(tmp_path: Path):
    _, service = _ingest(tmp_path, _project())
    by_id = {row["values"]["Observation id"]: row for row in _rows(service, "observations")}
    assert list(by_id) == ["obs-a", "obs-b", "obs-empty"]
    b = by_id["obs-b"]["values"]
    assert b["Media files"] == "clip-1.mp4 | clip-2.mp4", "file names only, never directories"
    assert b["Media file count"] == 2
    assert b["Media duration (s)"] == 60.0
    assert (b["Events"], b["First event (s)"], b["Last event (s)"]) == (6, 0.0, 20.0)
    assert b["Observation date"] == "2020-01-02T09:00:00"
    empty = by_id["obs-empty"]["values"]
    assert empty["Events"] == 0 and empty["First event (s)"] is None
    assert empty["Media duration (s)"] is None
    assert by_id["obs-a"]["source_row"] == {"observation_id": "obs-a"}


def test_an_unread_event_layout_leaves_event_tables_unprofiled(tmp_path: Path):
    document = _project({"o": {"events": [[1.0, "", "groom", "", "", 3, "extra"]]}})
    manifest, service = _ingest(tmp_path, document)
    for name in ("events", "intervals"):
        profile = manifest["tables"][f"{PROJECT}#{name}"]
        assert profile["profiled"] is False and profile["rows"] is None
        assert any("unread shape" in note for note in profile["warnings"])
        with pytest.raises(KeyError, match="not successfully profiled"):
            service.read_rows(f"{PROJECT}#{name}")
    assert any(f"{PROJECT}#events" in warning for warning in manifest["warnings"])
    # The observations table does not depend on the event layout.
    observations = manifest["tables"][f"{PROJECT}#observations"]
    assert observations["profiled"] is True and observations["rows"] == 1
    evidence = json.loads((tmp_path / "out" / "evidence.json").read_text(encoding="utf-8"))
    assert "boris.unprofiled" in json.dumps(evidence)


@pytest.mark.parametrize(
    "events",
    [
        [["1.0", "", "groom", "", ""]],  # a string time is not read as seconds
        [[1.0, "", "groom", ["mod"], ""]],  # modifiers as a list
        [[True, "", "groom", "", ""]],
        "not a list",
    ],
)
def test_other_unverified_layouts_are_not_guessed(events: object):
    tables = boris.build_tables(_project({"o": {"events": events}}))
    assert tables[boris.EVENTS].rows is None
    assert tables[boris.INTERVALS].rows is None
    assert tables[boris.OBSERVATIONS].rows is not None


def test_duration_aggregates_by_observation_and_behaviour(tmp_path: Path):
    _, service = _ingest(tmp_path, _project())
    result = service.aggregate(
        f"{PROJECT}#intervals",
        group_by=["Observation id", "Behavior"],
        metrics=[{"op": "sum", "column": "Duration (s)"}, {"op": "count"}],
    )
    got = {
        (group["group"]["Observation id"], group["group"]["Behavior"]): group["metrics"]
        for group in result["groups"]
    }
    # The open start counts as a bout, and adds nothing to the summed duration.
    assert got[("obs-b", "groom")] == {"sum:Duration (s)": 6.45, "count": 3}
    assert got[("obs-b", "rear")] == {"sum:Duration (s)": 0, "count": 1}
    assert got[("obs-a", "groom")] == {"sum:Duration (s)": 1.5, "count": 1}
    assert result["input"]["integrity"]["matches"] is True


def test_filter_rows_reads_the_intervals(tmp_path: Path):
    _, service = _ingest(tmp_path, _project())
    result = service.filter_rows(
        f"{PROJECT}#intervals",
        filters=[{"column": "Pairing", "op": "eq", "value": "unmatched_start"}],
    )
    assert result["matches_in_scanned_rows"] == 1
    assert result["rows"][0]["source_row"]["start_event_index"] == 5


def test_a_changed_project_is_refused(tmp_path: Path):
    _, service = _ingest(tmp_path, _project())
    changed = _project()
    changed["observations"]["obs-a"]["events"].append([9.0, "", "groom", "", ""])  # type: ignore[index]
    (tmp_path / "ds" / PROJECT).write_text(json.dumps(changed), encoding="utf-8")
    read = service.read_rows(f"{PROJECT}#intervals")
    assert read["integrity"]["matches"] is False
    assert read["rows"] == [] and "content_withheld" in read
    aggregated = service.aggregate(f"{PROJECT}#intervals", metrics=[{"op": "count"}])
    assert aggregated["input"]["integrity"]["matches"] is False


def test_the_project_path_itself_names_its_tables(tmp_path: Path):
    _, service = _ingest(tmp_path, _project())
    with pytest.raises(KeyError, match="BORIS project holding 3 table"):
        service.read_rows(PROJECT)
    with pytest.raises(KeyError, match="BORIS project holding 3 table"):
        service.inspect_table(PROJECT)


def test_the_manifest_holds_profiles_not_rows(tmp_path: Path):
    manifest, _ = _ingest(tmp_path, _project())
    profile = manifest["tables"][f"{PROJECT}#events"]
    assert profile["boris"] == {"file": PROJECT, "table": "events"}
    assert "rows" in profile and isinstance(profile["rows"], int)
    assert {column["name"] for column in profile["columns"]} == set(boris.EVENT_COLUMNS)
    dtypes = {column["name"]: column["dtype"] for column in profile["columns"]}
    assert dtypes["Time (s)"] == "number" and dtypes["Frame index"] == "integer"
    assert "videos" not in json.dumps(manifest), "media directories never reach the manifest"


def test_repeated_ingests_are_byte_identical(tmp_path: Path):
    _ingest(tmp_path, _project(), "a")
    _ingest(tmp_path, _project(), "b")
    first = (tmp_path / "a" / "manifest.json").read_bytes()
    assert first == (tmp_path / "b" / "manifest.json").read_bytes()


def test_the_manifest_validates_against_the_published_schema(tmp_path: Path):
    jsonschema = pytest.importorskip("jsonschema")
    source = tmp_path / "ds"
    source.mkdir()
    bad = _project({"o": {"events": [["x", "", "groom", "", ""]]}})
    (source / "unread.boris").write_text(json.dumps(bad), encoding="utf-8")
    manifest, _ = _ingest(tmp_path, _project())
    assert manifest["tables"]["unread.boris#events"]["profiled"] is False
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "dataset-manifest.schema.json"
    jsonschema.validate(manifest, json.loads(schema_path.read_text(encoding="utf-8")))
