"""Declared identifier crosswalks: explicit, cited, never implicit.

Every identifier here is synthetic. The shapes mirror a real failure -- a
registry that splits an animal ID into cage and tail columns and writes the
cage with a dash, and measurement files that write one concatenated ID without
it -- but no value comes from a real dataset.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from data2agent.cli import main
from data2agent.errors import OutputError
from data2agent.ingest import ingest
from data2agent.mcp import DatasetService
from data2agent.relationships import CrosswalkError, load_crosswalk_file, parse_crosswalk
from data2agent.relationships.crosswalk import parse_key_format

REGISTRY = "cage,tail,group\nZ-7,L,ctrl\nZ-7,R,test\nZ-8,L,test\nZ-9,L,ctrl\n"
SESSIONS = "animal,session,score\nZ7-L,1,3.5\nZ7-L,2,4.0\nZ7-R,1,2.0\nZ8-L,1,1.5\nQ5-X,1,9.9\n"
CROSSWALK = (
    "canonical_id,form,note\n"
    "Z-7_L,Z-7-L,registry cage-tail rendering\n"
    "Z-7_L,Z7-L,measurement spelling\n"
    "Z-7_R,Z-7-R,\n"
    "Z-7_R,Z7-R,\n"
    "Z-8_L,Z-8-L,\n"
    "Z-8_L,Z8-L,\n"
)
DECLARATION = {
    "left": "registry.csv",
    "right": "sessions.csv",
    "left_keys": ["cage", "tail"],
    "right_keys": ["animal"],
    "left_key_format": "{cage}-{tail}",
    "key_crosswalk": "ids",
    "expected_cardinality": "one_to_many",
}


def _dataset(tmp_path: Path, sessions: str = SESSIONS, crosswalk: str = CROSSWALK):
    source = tmp_path / "source"
    source.mkdir()
    (source / "registry.csv").write_text(REGISTRY, encoding="utf-8")
    (source / "sessions.csv").write_text(sessions, encoding="utf-8")
    crosswalk_path = tmp_path / "ids.csv"
    crosswalk_path.write_text(crosswalk, encoding="utf-8")
    result = ingest(source, tmp_path / "out")
    return result.output_dir, crosswalk_path


def _build(output: Path, crosswalk_path: Path, declarations=None) -> dict:
    builder = DatasetService(output, load_relationships=False)
    bundle = builder.build_relationships(
        declarations if declarations is not None else [DECLARATION],
        crosswalks=[load_crosswalk_file(crosswalk_path, name="ids")],
    )
    (output / "relationships.json").write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    return bundle


def _declared(bundle: dict) -> dict:
    (relation,) = [
        record
        for record in bundle["relationships"]
        if record["basis"]["method"] != "shared-normalised-column"
    ]
    return relation


# -- the file format ---------------------------------------------------------


def test_a_form_mapped_to_two_canonical_ids_is_rejected_naming_both():
    with pytest.raises(CrosswalkError) as caught:
        parse_crosswalk("canonical_id,form\nA-1,a1\nB-2,a1\n", name="ids")
    message = str(caught.value)
    assert "'a1' maps to more than one canonical_id" in message
    assert "'A-1' (line(s) [2])" in message and "'B-2' (line(s) [3])" in message


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("canonical_id,form\nA-1,\n", "line 2: empty form"),
        ("canonical_id,form\n ,a1\n", "line 2: empty canonical_id"),
        ("canonical_id,form\nA-1,a1\nA-1,a1\n", "listed more than once"),
        ("canonical_id,alias\nA-1,a1\n", "lacks required column(s) ['form']"),
        ("canonical_id,form,guess\nA-1,a1,x\n", "unknown column(s) ['guess']"),
        ("canonical_id,form\nA-1,a1,extra\n", "3 field(s) where the header has 2"),
        ("", "is empty"),
        ("canonical_id,form\n", "lists no forms"),
    ],
)
def test_crosswalk_validation_is_strict(content: str, expected: str):
    with pytest.raises(CrosswalkError, match=None) as caught:
        parse_crosswalk(content, name="ids")
    assert expected in str(caught.value)


def test_values_are_kept_verbatim_and_the_hash_is_of_the_file_bytes(tmp_path: Path):
    raw = "﻿canonical_id,form\r\nA-1, a1\r\nA-1,A1\r\n".encode()
    path = tmp_path / "ids.csv"
    path.write_bytes(raw)
    crosswalk = load_crosswalk_file(path)
    assert crosswalk.name == "ids.csv"
    assert crosswalk.sha256 == hashlib.sha256(raw).hexdigest()
    # No stripping or case folding: " a1" and "A1" are the listed forms, "a1" is not.
    assert crosswalk.lookup(" a1") == "A-1"
    assert crosswalk.lookup("A1") == "A-1"
    assert crosswalk.lookup("a1") is None
    # The canonical ID is not implicitly one of its own forms.
    assert crosswalk.lookup("A-1") is None


def test_key_format_must_use_exactly_the_declared_key_columns():
    with pytest.raises(CrosswalkError, match="does not use left_keys column"):
        parse_key_format("{cage}", ["cage", "tail"], side="left")
    with pytest.raises(CrosswalkError, match="not in left_keys"):
        parse_key_format("{cage}-{tail}-{sex}", ["cage", "tail"], side="left")
    rendered = parse_key_format("{{{cage}}}-{tail}", ["cage", "tail"], side="left")
    assert rendered.render({"cage": "Z-7", "tail": "L"}) == "{Z-7}-L"


# -- assessment through the crosswalk -----------------------------------------


def test_exact_equality_finds_nothing_so_the_declaration_is_rejected(tmp_path: Path):
    output, _ = _dataset(tmp_path)
    bundle = DatasetService(output, load_relationships=False).build_relationships(
        [
            {
                "left": "registry.csv",
                "right": "sessions.csv",
                "left_keys": ["cage", "tail"],
                "right_keys": ["animal"],
                "left_key_format": "{cage}-{tail}",
            }
        ]
    )
    relation = _declared(bundle)
    assert relation["status"] == "rejected"
    assert relation["key_mapping"]["crosswalk"] is None


def test_relationship_is_assessed_on_canonical_ids(tmp_path: Path):
    output, crosswalk_path = _dataset(tmp_path)
    bundle = _build(output, crosswalk_path)
    relation = _declared(bundle)

    assert relation["status"] == "declared"
    assert relation["cardinality"] == "one_to_many"
    assert relation["matched_distinct_keys"] == 3
    assert relation["joined_row_count"] == 4
    assert relation["evidence"]["examples"][0] == {
        "key": ["Z-7_L"],
        "mapping": "crosswalk",
        "left_source_rows": [2],
        "right_source_rows": [2, 3],
    }

    mapping = relation["key_mapping"]
    assert mapping["crosswalk"] == {"name": "ids", "sha256": bundle["crosswalks"][0]["sha256"]}
    left, right = mapping["left"], mapping["right"]
    assert left["key_format"] == "{cage}-{tail}"
    assert (left["mapped_rows"], left["unmapped_rows"]) == (3, 1)
    assert left["unmapped_examples"] == [{"value": "Z-9-L", "source_rows": [5]}]
    assert (right["mapped_rows"], right["unmapped_rows"]) == (4, 1)
    assert right["mapped_distinct_values"] == 3
    assert (left["unmatched_distinct_keys"], right["unmatched_distinct_keys"]) == (1, 1)
    assert left["collisions"] == [] and right["collisions"] == []
    assert any("absent from the crosswalk" in warning for warning in relation["warnings"])


def test_cardinality_is_computed_on_canonical_ids_not_raw_spellings(tmp_path: Path):
    # Two spellings of one animal on the right, one per row: raw keys are
    # unique (one_to_one by spelling), canonical IDs are not (one_to_many).
    sessions = "animal,session\nZ7-L,1\nZ-7-L,2\n"
    output, crosswalk_path = _dataset(tmp_path, sessions=sessions)
    declaration = {**DECLARATION, "expected_cardinality": "one_to_one"}
    relation = _declared(_build(output, crosswalk_path, [declaration]))
    assert relation["cardinality"] != "one_to_one"
    assert relation["status"] == "rejected"


def test_two_forms_of_one_canonical_id_in_one_table_are_a_collision_not_a_merge(tmp_path: Path):
    sessions = "animal,session\nZ7-L,1\nZ-7-L,1\nZ7-R,1\n"
    output, crosswalk_path = _dataset(tmp_path, sessions=sessions)
    relation = _declared(_build(output, crosswalk_path))

    assert relation["status"] == "rejected"
    assert any("crosswalk collision on the right side" in r for r in relation["rejection_reasons"])
    assert relation["key_mapping"]["right"]["collisions"] == [
        {
            "canonical_id": "Z-7_L",
            "forms": [
                {"form": "Z-7-L", "rows": 1, "source_rows": [3]},
                {"form": "Z7-L", "rows": 1, "source_rows": [2]},
            ],
        }
    ]
    service = DatasetService(output)
    with pytest.raises(ValueError, match="only declared or deterministic"):
        service.join_relationship(relation["id"])
    adhoc = service.join_tables(
        "registry.csv",
        "sessions.csv",
        left_keys=["cage", "tail"],
        right_keys=["animal"],
        left_key_format="{cage}-{tail}",
        crosswalk="ids",
    )
    assert adhoc["rows"] == []
    assert "crosswalk collision on right" in adhoc["content_withheld"]


def test_an_unlisted_value_equal_to_a_canonical_id_does_not_join_it(tmp_path: Path):
    sessions = "animal,session\nZ-8_L,1\n"
    output, crosswalk_path = _dataset(tmp_path, sessions=sessions)
    relation = _declared(_build(output, crosswalk_path, [{**DECLARATION}]))
    right = relation["key_mapping"]["right"]
    assert right["unmapped_values_equal_to_a_canonical_id"] == ["Z-8_L"]
    assert relation["matched_distinct_keys"] == 0
    assert relation["status"] == "rejected"


def test_a_declaration_cannot_name_an_unsupplied_crosswalk(tmp_path: Path):
    output, _ = _dataset(tmp_path)
    with pytest.raises(ValueError, match="names key_crosswalk 'ids', which was not supplied"):
        DatasetService(output, load_relationships=False).build_relationships([DECLARATION])


def test_a_composite_key_needs_a_declared_format_to_use_a_crosswalk(tmp_path: Path):
    output, crosswalk_path = _dataset(tmp_path)
    declaration = {key: value for key, value in DECLARATION.items() if key != "left_key_format"}
    with pytest.raises(ValueError, match="left_keys/right_keys must be non-empty and equal"):
        _build(output, crosswalk_path, [declaration])


# -- serving -------------------------------------------------------------------


def test_join_relationship_returns_raw_keys_and_the_canonical_id(tmp_path: Path):
    output, crosswalk_path = _dataset(tmp_path)
    relation = _declared(_build(output, crosswalk_path))
    service = DatasetService(output)

    joined = service.join_relationship(
        relation["id"], left_columns=["group"], right_columns=["score"]
    )
    assert joined["total_result_rows"] == 4
    sha256 = hashlib.sha256(crosswalk_path.read_bytes()).hexdigest()
    assert joined["relationship_contract"]["crosswalk"] == {"name": "ids", "sha256": sha256}
    assert joined["key_mapping"]["crosswalk"]["declared_in"] == "relationships.json"
    first = joined["rows"][0]
    assert first["key"] == {
        "left_raw": ["Z-7", "L"],
        "right_raw": ["Z7-L"],
        "key": ["Z-7_L"],
        "canonical_id": "Z-7_L",
        "mapping": "crosswalk",
    }
    assert first["left"] == {"group": "ctrl"}
    assert first["right"] == {"score": 3.5}

    left_join = service.join_relationship(relation["id"], how="left", limit=10)
    unmatched = [row for row in left_join["rows"] if row["right"] is None]
    assert [row["key"]["left_raw"] for row in unmatched] == [["Z-9", "L"]]
    assert unmatched[0]["key"]["mapping"] == "unmapped"
    assert unmatched[0]["key"]["canonical_id"] is None


def test_unmapped_values_pass_through_and_match_only_identical_values(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.csv").write_text("id,x\nZ7-L,1\nQ5-X,2\n", encoding="utf-8")
    (source / "b.csv").write_text("subject,y\nZ-7-L,10\nQ5-X,20\nq5-x,30\n", encoding="utf-8")
    crosswalk_path = tmp_path / "ids.csv"
    crosswalk_path.write_text(CROSSWALK, encoding="utf-8")
    output = ingest(source, tmp_path / "out").output_dir
    _build(output, crosswalk_path, declarations=[])

    joined = DatasetService(output).join_tables(
        "a.csv", "b.csv", left_keys=["id"], right_keys=["subject"], crosswalk="ids"
    )
    pairs = sorted((row["left"]["x"], row["right"]["y"]) for row in joined["rows"])
    assert pairs == [(1, 10), (2, 20)]  # q5-x is never guessed to be Q5-X
    assert joined["key_mapping"]["right"]["unmapped_rows"] == 2
    assert any("passed through unchanged" in warning for warning in joined["warnings"])


def test_join_tables_accepts_only_a_crosswalk_declared_in_the_bundle(tmp_path: Path):
    output, _ = _dataset(tmp_path)
    service = DatasetService(output)
    with pytest.raises(KeyError, match="no crosswalk named 'ids' is declared"):
        service.join_tables(
            "registry.csv",
            "sessions.csv",
            left_keys=["cage", "tail"],
            right_keys=["animal"],
            left_key_format="{cage}-{tail}",
            crosswalk="ids",
        )


def test_a_changed_crosswalk_file_invalidates_the_bundle(tmp_path: Path):
    output, crosswalk_path = _dataset(tmp_path)
    _build(output, crosswalk_path)
    assert (
        DatasetService(output).list_relationships()["crosswalks"][0]["source_file"]["status"]
        == "matches"
    )

    crosswalk_path.write_text(CROSSWALK + "Z-9_L,Z-9-L,\n", encoding="utf-8")
    with pytest.raises(OutputError, match="changed after relationships.json was built"):
        DatasetService(output)


def test_a_moved_crosswalk_file_serves_the_cited_inline_copy(tmp_path: Path):
    output, crosswalk_path = _dataset(tmp_path)
    relation = _declared(_build(output, crosswalk_path))
    crosswalk_path.unlink()
    service = DatasetService(output)
    listed = service.list_relationships()["crosswalks"][0]
    assert listed["source_file"]["status"].startswith("not re-checked")
    assert service.join_relationship(relation["id"])["total_result_rows"] == 4


def test_an_edited_inline_crosswalk_is_refused(tmp_path: Path):
    output, crosswalk_path = _dataset(tmp_path)
    bundle = _build(output, crosswalk_path)
    bundle["crosswalks"][0]["content"] = bundle["crosswalks"][0]["content"].replace(
        "Z-8_L,Z8-L", "Z-7_L,Z8-L"
    )
    (output / "relationships.json").write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(OutputError, match="no longer hashes to its recorded sha256"):
        DatasetService(output)


def test_resolve_identifier_reports_crosswalk_membership(tmp_path: Path):
    output, crosswalk_path = _dataset(tmp_path)
    _build(output, crosswalk_path)
    service = DatasetService(output)
    (member,) = service.resolve_identifier("Z7-L")["crosswalk_membership"]
    assert member["role"] == "form"
    assert member["canonical_id"] == "Z-7_L"
    assert member["forms"] == ["Z-7-L", "Z7-L"]
    assert service.resolve_identifier("z7-l")["crosswalk_membership"] == []


def test_bundle_with_a_crosswalk_matches_its_schema(tmp_path: Path):
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "relationships.schema.json"
    output, crosswalk_path = _dataset(tmp_path)
    bundle = _build(output, crosswalk_path)
    jsonschema.validate(bundle, json.loads(schema_path.read_text(encoding="utf-8")))


# -- CLI -----------------------------------------------------------------------


def test_cli_registers_a_named_crosswalk(tmp_path: Path, capsys):
    output, crosswalk_path = _dataset(tmp_path)
    declarations = tmp_path / "rel.json"
    declarations.write_text(json.dumps([DECLARATION]), encoding="utf-8")
    code = main(
        [
            "relationships",
            str(output),
            "--declarations",
            str(declarations),
            "--crosswalk",
            f"ids={crosswalk_path}",
        ]
    )
    assert code == 0
    assert "crosswalk     : ids (6 forms -> 3 canonical IDs" in capsys.readouterr().out
    bundle = json.loads((output / "relationships.json").read_text(encoding="utf-8"))
    assert _declared(bundle)["status"] == "declared"


def test_cli_refuses_a_conflicting_crosswalk(tmp_path: Path, capsys):
    output, crosswalk_path = _dataset(tmp_path, crosswalk=CROSSWALK + "Z-8_L,Z7-L,\n")
    code = main(["relationships", str(output), "--crosswalk", str(crosswalk_path)])
    assert code == 2
    assert "'Z7-L' maps to more than one canonical_id" in capsys.readouterr().err
    assert not (output / "relationships.json").exists()


AMBIGUOUS = "cage,tail,x\nc1,23,1\nc12,3,2\n"


def _ambiguous(tmp_path: Path, crosswalk: str | None):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.csv").write_text(AMBIGUOUS, encoding="utf-8")
    (source / "b.csv").write_text("animal,y\nc123,10\n", encoding="utf-8")
    output = ingest(source, tmp_path / "out").output_dir
    crosswalks = []
    if crosswalk is not None:
        path = tmp_path / "ids.csv"
        path.write_text(crosswalk, encoding="utf-8")
        crosswalks = [load_crosswalk_file(path, name="ids")]
    declaration = {
        "left": "a.csv",
        "right": "b.csv",
        "left_keys": ["cage", "tail"],
        "right_keys": ["animal"],
        "left_key_format": "{cage}{tail}",
    }
    if crosswalk is not None:
        declaration["key_crosswalk"] = "ids"
    bundle = DatasetService(output, load_relationships=False).build_relationships(
        [declaration], crosswalks=crosswalks
    )
    (output / "relationships.json").write_text(json.dumps(bundle), encoding="utf-8")
    return output, _declared(bundle)


@pytest.mark.parametrize(
    "crosswalk", [None, "canonical_id,form\nS-1,c123\n"], ids=["no-crosswalk", "crosswalk"]
)
def test_a_non_injective_key_format_is_a_rendering_collision_not_a_merge(
    tmp_path: Path, crosswalk: str | None
):
    output, relation = _ambiguous(tmp_path, crosswalk)

    assert relation["status"] == "rejected"
    assert any("rendering collision on the left side" in r for r in relation["rejection_reasons"])
    assert any("no separator" in warning for warning in relation["warnings"])
    assert relation["key_mapping"]["left"]["rendering_collisions"] == [
        {
            "rendered": "c123",
            "raw_keys": [
                {"raw": ["c1", 23], "source_rows": [2]},
                {"raw": ["c12", 3], "source_rows": [3]},
            ],
        }
    ]
    service = DatasetService(output)
    with pytest.raises(ValueError, match="only declared or deterministic"):
        service.join_relationship(relation["id"])
    adhoc = service.join_tables(
        "a.csv",
        "b.csv",
        left_keys=["cage", "tail"],
        right_keys=["animal"],
        left_key_format="{cage}{tail}",
        crosswalk="ids" if crosswalk is not None else None,
    )
    assert adhoc["rows"] == []
    assert "rendering collision on left" in adhoc["content_withheld"]


def test_empty_keys_with_a_crosswalk_fail_validation_not_with_an_index_error(tmp_path: Path):
    output, crosswalk_path = _dataset(tmp_path)
    _build(output, crosswalk_path)
    with pytest.raises(ValueError, match="left_keys and right_keys must be non-empty"):
        DatasetService(output).join_tables(
            "registry.csv", "sessions.csv", left_keys=[], right_keys=["animal"], crosswalk="ids"
        )


def test_a_key_format_alone_renders_a_composite_key_for_exact_comparison(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.csv").write_text("batch,slot,x\n3,7,1\n3,8,2\n", encoding="utf-8")
    (source / "b.csv").write_text("code,y\n3/7,10\n3/9,20\n", encoding="utf-8")
    output = ingest(source, tmp_path / "out").output_dir
    relation = _declared(
        DatasetService(output, load_relationships=False).build_relationships(
            [
                {
                    "left": "a.csv",
                    "right": "b.csv",
                    "left_keys": ["batch", "slot"],
                    "right_keys": ["code"],
                    "left_key_format": "{batch}/{slot}",
                }
            ]
        )
    )
    assert relation["status"] == "declared"
    assert relation["matched_distinct_keys"] == 1
    assert relation["key_mapping"]["crosswalk"] is None
    assert relation["evidence"]["examples"][0]["key"] == ["3/7"]
