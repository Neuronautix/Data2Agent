"""Free-text columns keep their counts but not their value lists (D2A-110).

All data here is synthetic: invented codes and invented sentences.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data2agent.errors import LayoutError
from data2agent.ingest import ingest
from data2agent.ingest.layout import parse_declarations
from data2agent.ingest.tabular import FREE_TEXT_RULE_ID
from data2agent.mcp import DatasetService

LONG = "x123456789" * 4 + "x"  # 41 characters, one token
TABLE = (
    "animal,genotype,group,visit,note,bad_take,flag,path\n"
    "a1,WT,Line3 KO,2020-01-01 09:00:00,calm and alert during the session,ok,re do,x\n"
    "a2,KO,Line3 KO,2020-01-02 09:00:00,,bad video,ok,x\n"
    f"a3,WT,Line3 WT,2020-01-03 09:00:00,moved before the recording started,ok,re do,{LONG}\n"
    "a4,KO,Line3 WT,2020-01-04 09:00:00,,ok,ok,x\n"
)


def _ingest(tmp_path: Path, declaration: dict | None = None, name: str = "out") -> dict:
    source = tmp_path / "ds"
    source.mkdir(exist_ok=True)
    (source / "animals.csv").write_text(TABLE, encoding="utf-8")
    layouts = (
        parse_declarations(declaration, sha256="0" * 64, name="decl.json")
        if declaration is not None
        else None
    )
    return ingest(source, tmp_path / name, layouts=layouts).manifest


def _columns(manifest: dict, table: str = "animals.csv") -> dict[str, dict]:
    return {column["name"]: column for column in manifest["tables"][table]["columns"]}


def test_coded_columns_keep_their_value_lists(tmp_path: Path):
    columns = _columns(_ingest(tmp_path))
    assert columns["genotype"]["distinct_values"] == ["KO", "WT"]
    # Two words per value, but each value recurs: a vocabulary, not a note.
    assert columns["group"]["distinct_values"] == ["Line3 KO", "Line3 WT"]
    assert columns["flag"]["distinct_values"] == ["ok", "re do"]
    # Dates and times hold no word at all.
    assert len(columns["visit"]["distinct_values"]) == 4
    for name in ("genotype", "group", "flag", "visit"):
        assert "values_withheld" not in columns[name]


def test_prose_is_withheld_with_the_rule_and_its_statistics(tmp_path: Path):
    manifest = _ingest(tmp_path)
    note = _columns(manifest)["note"]
    assert "distinct_values" not in note
    assert note["values_withheld"] == {
        "reason": "free_text",
        "rule": FREE_TEXT_RULE_ID,
        "clause": "prose",
        "max_words": 6,
        "max_length": 34,
        "unrepeated_multiword_values": 2,
    }
    # Every count is kept; only the list is gone.
    assert (note["distinct"], note["distinct_exact"], note["missing"]) == (2, True, 2)
    assert "calm and alert" not in json.dumps(manifest)


def test_a_short_note_written_once_is_withheld(tmp_path: Path):
    manifest = _ingest(tmp_path)
    withheld = _columns(manifest)["bad_take"]["values_withheld"]
    assert withheld["clause"] == "unrepeated-words"
    assert withheld["unrepeated_multiword_values"] == 1
    assert "bad video" not in json.dumps(manifest)


def test_one_long_token_is_withheld(tmp_path: Path):
    withheld = _columns(_ingest(tmp_path))["path"]["values_withheld"]
    assert (withheld["clause"], withheld["max_length"]) == ("prose", 41)


def test_withheld_values_stay_readable_from_the_verified_file(tmp_path: Path):
    _ingest(tmp_path)
    service = DatasetService(tmp_path / "out")
    rows = service.read_rows("animals.csv", columns=["note"])["rows"]
    assert rows[0]["values"]["note"] == "calm and alert during the session"
    matches = service.filter_rows(
        "animals.csv", filters=[{"column": "bad_take", "op": "eq", "value": "bad video"}]
    )
    assert matches["matches_in_scanned_rows"] == 1


def test_a_declaration_can_withhold_a_coded_column(tmp_path: Path):
    manifest = _ingest(tmp_path, {"free_text": {"animals.csv": {"genotype": True}}})
    genotype = _columns(manifest)["genotype"]
    assert "distinct_values" not in genotype
    assert genotype["values_withheld"] == {
        "reason": "free_text",
        "rule": "declared",
        "source": "declaration",
        "max_words": 1,
        "max_length": 2,
        "unrepeated_multiword_values": 0,
    }
    assert manifest["layout_declaration"]["free_text_tables"] == ["animals.csv"]


def test_a_declaration_can_list_a_column_the_rule_withholds(tmp_path: Path):
    manifest = _ingest(tmp_path, {"free_text": {"animals.csv": {"bad_take": False}}})
    column = _columns(manifest)["bad_take"]
    assert column["distinct_values"] == ["bad video", "ok"]
    assert "values_withheld" not in column
    assert column["values_listed_by"] == {
        "rule": "declared",
        "source": "declaration",
        "structural_rule": FREE_TEXT_RULE_ID,
    }


def test_a_declaration_naming_no_column_is_refused(tmp_path: Path):
    with pytest.raises(LayoutError, match="free_text"):
        _ingest(tmp_path, {"free_text": {"animals.csv": {"remarks": True}}})
    with pytest.raises(LayoutError, match="true or false"):
        parse_declarations(
            {"free_text": {"animals.csv": {"note": "yes"}}}, sha256="0" * 64, name="d"
        )


def test_boris_comments_and_descriptions_never_reach_the_manifest(tmp_path: Path):
    project = {
        "project_format_version": "7.0",
        "behaviors_conf": {"0": {"code": "hind scratch", "type": "State event", "category": ""}},
        "subjects_conf": {},
        "observations": {
            "obs-1": {
                "description": "prosecret",  # one word: withheld because BORIS says so
                "events": [
                    [1.0, "", "hind scratch", "", "comsecret"],
                    [2.0, "", "hind scratch", "", ""],
                ],
            }
        },
    }
    source = tmp_path / "ds"
    source.mkdir()
    (source / "p.boris").write_text(json.dumps(project), encoding="utf-8")
    manifest = ingest(source, tmp_path / "out").manifest
    text = json.dumps(manifest)
    assert "comsecret" not in text and "prosecret" not in text
    events = _columns(manifest, "p.boris#events")
    assert events["Comment"]["values_withheld"]["source"] == "boris"
    # A two-word ethogram code is still a code: BORIS defines it, so it is listed.
    assert events["Behavior"]["distinct_values"] == ["hind scratch"]
    intervals = _columns(manifest, "p.boris#intervals")
    assert intervals["Behavior"]["values_listed_by"]["source"] == "boris"
    assert intervals["Comment start"]["values_withheld"]["rule"] == "declared"
    observations = _columns(manifest, "p.boris#observations")
    assert observations["Description"]["values_withheld"]["source"] == "boris"


def test_repeated_ingests_are_byte_identical(tmp_path: Path):
    _ingest(tmp_path, name="a")
    _ingest(tmp_path, name="b")
    assert (tmp_path / "a" / "manifest.json").read_bytes() == (
        tmp_path / "b" / "manifest.json"
    ).read_bytes()


def test_the_manifest_validates_against_the_published_schema(tmp_path: Path):
    jsonschema = pytest.importorskip("jsonschema")
    manifest = _ingest(
        tmp_path, {"free_text": {"animals.csv": {"bad_take": False, "genotype": True}}}
    )
    source = tmp_path / "ds"
    project = {
        "project_format_version": "7.0",
        "behaviors_conf": {"0": {"code": "hind scratch", "type": "Point event"}},
        "observations": {"o": {"description": "x", "events": [[1.0, "", "hind scratch", "", "c"]]}},
    }
    (source / "p.boris").write_text(json.dumps(project), encoding="utf-8")
    both = ingest(source, tmp_path / "out2").manifest
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "dataset-manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(manifest, schema)
    jsonschema.validate(both, schema)
