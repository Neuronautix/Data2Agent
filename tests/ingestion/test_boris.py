"""BORIS project files: detected by confirmed content, profiled by counts only.

Every project here is synthetic: a minimal object with BORIS's project layout
and invented placeholder names. No real project is used or needed.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from data2agent.ingest import formats, ingest


def _project(**overrides: object) -> dict[str, object]:
    project: dict[str, object] = {
        "project_format_version": "7.0",
        "project_name": "synthetic",
        "project_description": "free text that must not reach the manifest",
        "time_format": "hh:mm:ss",
        "behaviors_conf": {
            "0": {"code": "behaviour-a", "type": "State event"},
            "1": {"code": "behaviour-b", "type": "Point event"},
        },
        "subjects_conf": {"0": {"name": "subject-a"}},
        "observations": {
            "obs-1": {"events": [[1.0, "subject-a", "behaviour-a", "", ""]]},
            "obs-2": {"events": []},
            "obs-3": {"events": []},
        },
        "independent_variables": {},
    }
    project.update(overrides)
    return project


def _write(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_a_boris_project_is_detected_by_its_content(tmp_path: Path):
    detected, notes = formats.detect(_write(tmp_path / "project.boris", _project()))
    assert detected.format_id == "boris"
    assert detected.media_type == "application/x-boris+json"
    assert detected.detected_by == "content"
    assert detected.extension_format == "boris"
    assert detected.extension_conflict is False
    assert notes == []


def test_an_older_project_without_optional_sections_is_still_boris(tmp_path: Path):
    """Only the version-stable keys are required, not today's full key set."""
    minimal = {"project_format_version": 4, "behaviors_conf": {}, "observations": {}}
    detected, _ = formats.detect(_write(tmp_path / "old.boris", minimal))
    assert detected.format_id == "boris"


def test_a_utf8_bom_does_not_hide_a_project(tmp_path: Path):
    path = tmp_path / "bom.boris"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_project()).encode("utf-8"))
    assert formats.detect(path)[0].format_id == "boris"


@pytest.mark.parametrize(
    "document",
    [
        {"observations": {}, "behaviors_conf": {}},  # no format version
        {"project_format_version": "7.0"},
        [1, 2, 3],
    ],
)
def test_json_of_another_shape_is_not_claimed_as_boris(tmp_path: Path, document: object):
    detected, notes = formats.detect(_write(tmp_path / "other.boris", document))
    assert detected.format_id == "json"
    assert detected.extension_format == "boris"
    assert detected.extension_conflict is True
    assert any("BORIS project keys" in note for note in notes)


@pytest.mark.parametrize("payload", [b"not json at all", b"\x00\x01\x02\xff", b""])
def test_a_boris_name_over_non_json_stays_unknown(tmp_path: Path, payload: bytes):
    path = tmp_path / "broken.boris"
    path.write_bytes(payload)
    detected, notes = formats.detect(path)
    assert detected.format_id == "unknown"
    assert detected.detected_by == "none"
    assert detected.extension_format == "boris"
    assert detected.extension_conflict is True
    assert notes


def test_gzip_bytes_under_a_boris_name_are_reported_as_gzip(tmp_path: Path):
    """The container is not opened: the bytes say gzip, and the name disagrees."""
    path = tmp_path / "packed.boris"
    path.write_bytes(gzip.compress(json.dumps(_project()).encode("utf-8"), mtime=0))
    detected, notes = formats.detect(path)
    assert detected.format_id == "gzip"
    assert detected.extension_conflict is True
    assert notes


def test_a_json_file_holding_a_project_stays_json(tmp_path: Path):
    detected, _ = formats.detect(_write(tmp_path / "project.json", _project()))
    assert detected.format_id == "json"
    assert detected.extension_conflict is False


def _ingest(tmp_path: Path, files: dict[str, object]) -> dict:
    source = tmp_path / "ds"
    source.mkdir(exist_ok=True)
    for name, document in files.items():
        _write(source / name, document)
    out = tmp_path / "out"
    ingest(source, out)
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def test_the_manifest_records_counts_and_never_contents(tmp_path: Path):
    manifest = _ingest(tmp_path, {"project.boris": _project()})
    (entry,) = manifest["files"]
    assert entry["format"] == "boris"
    assert entry["detected_by"] == "content"
    assert manifest["structured"]["project.boris"]["boris"] == {
        "project_format_version": "7.0",
        "observations": 3,
        "subjects": 1,
        "behaviors": 2,
    }
    text = json.dumps(manifest)
    for private in ("subject-a", "behaviour-a", "obs-1", "free text"):
        assert private not in text
    assert not any("not recognised" in warning for warning in manifest["warnings"])


def test_an_absent_section_is_null_not_zero(tmp_path: Path):
    project = _project(subjects_conf=None)
    manifest = _ingest(tmp_path, {"project.boris": project})
    assert manifest["structured"]["project.boris"]["boris"]["subjects"] is None


def test_plain_json_gets_no_boris_summary(tmp_path: Path):
    manifest = _ingest(tmp_path, {"project.json": _project(), "other.boris": [1]})
    assert "boris" not in manifest["structured"]["project.json"]
    assert "boris" not in manifest["structured"]["other.boris"]


def test_the_manifest_validates_against_the_published_schema(tmp_path: Path):
    jsonschema = pytest.importorskip("jsonschema")
    source = tmp_path / "ds"
    source.mkdir()
    (source / "broken.boris").write_bytes(b"\x00")
    manifest = _ingest(
        tmp_path,
        {
            "project.boris": _project(),
            "sparse.boris": _project(subjects_conf=None, project_format_version=[1]),
            "other.boris": [1],
            "project.json": _project(),
        },
    )
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "dataset-manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(manifest, schema)
    assert manifest["structured"]["sparse.boris"]["boris"]["project_format_version"] is None


def test_repeated_ingests_are_byte_identical(tmp_path: Path):
    source = tmp_path / "ds"
    source.mkdir()
    _write(source / "project.boris", _project())
    (source / "broken.boris").write_bytes(b"\x00")
    first, second = tmp_path / "a", tmp_path / "b"
    ingest(source, first)
    ingest(source, second)
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
