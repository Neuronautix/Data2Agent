"""Every refusal the exporter promises, plus the CLI and the layering rule."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from export_helpers import (
    AGENT_MODEL,
    CREATED_AT,
    DATASET_PATH,
    FAMILY,
    read_proposed,
    result,
)

from data2agent.cli import main
from data2agent.export import (
    ExportInternalError,
    ExportRefused,
    build_annotations,
)
from data2agent.export.annotations import assert_proposal_invariants, write_records

pytest.importorskip("yaml")


@pytest.fixture
def assessed(write_assessment):
    write_assessment(
        "fair",
        [
            result("F1-PID-METADATA", "F1", "pass"),
            result("F2-METADATA-PRESENT", "F2", "fail", rationale="none found"),
        ],
    )


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# -- --dataset-path --------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "/etc",
        "C:/Windows",
        "C:\\Windows",
        "\\\\server\\share",
        "../outside",
        "data/../../outside",
        "data/original/..",
        "..",
        "data\\original\\synthfam",
        ".",
        "data//original",
        "data/original/synth fam",
        "",
        "   ",
    ],
)
def test_a_dangerous_dataset_path_is_refused(export, dataspace, assessed, bad):
    with pytest.raises(ExportRefused):
        export(dataset_path=bad)
    assert read_proposed(dataspace) == {}


def test_an_absolute_dataset_path_is_refused_even_when_it_points_at_the_dataset(
    export, dataspace, assessed
):
    with pytest.raises(ExportRefused, match="absolute"):
        export(dataset_path=str(dataspace / DATASET_PATH))


def test_a_dataset_path_that_does_not_exist_is_refused(export, dataspace, assessed):
    with pytest.raises(ExportRefused, match="does not exist"):
        export(dataset_path="data/original/ghost")
    assert read_proposed(dataspace) == {}


def test_a_dataset_path_that_is_a_file_is_refused(export, dataspace, assessed):
    with pytest.raises(ExportRefused, match="does not exist as a directory"):
        export(dataset_path=f"{DATASET_PATH}/t.csv")


def test_a_trailing_slash_is_normalised(export, dataspace, assessed):
    export(dataset_path=DATASET_PATH + "/")
    record = read_proposed(dataspace)["synthfam-original-F1-pid-metadata"]
    assert record["target"]["path"] == DATASET_PATH


def test_a_dataspace_that_is_not_a_directory_is_refused(export, dataspace, assessed):
    with pytest.raises(ExportRefused, match="not a directory"):
        export(dataspace=dataspace / "nope")


def test_a_symlink_escaping_the_dataspace_is_refused(export, dataspace, tmp_path, assessed):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = dataspace / "data" / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create symlinks without privileges")
    with pytest.raises(ExportRefused, match="outside the dataspace"):
        export(dataset_path="data/escape")


# -- output directory ---------------------------------------------------------------------


def test_only_annotations_proposed_is_ever_written(export, dataspace, assessed):
    before = _tree_digest(dataspace / "data")
    export()
    assert sorted(p.name for p in (dataspace / "annotations" / "curated").iterdir()) == []
    assert _tree_digest(dataspace / "data") == before  # data/original untouched
    written = {
        p.relative_to(dataspace).as_posix()
        for p in dataspace.rglob("synthfam-*.json")
        if p.is_file()
    }
    assert written
    assert all(name.startswith("annotations/proposed/") for name in written)


def test_writing_to_any_other_directory_is_refused(dataspace, ingest_dir, assessed):
    built = build_annotations(
        ingest_dir,
        family=FAMILY,
        variant="original",
        dataset_path=DATASET_PATH,
        agent_model=AGENT_MODEL,
        created_at=CREATED_AT,
    )
    root = dataspace.resolve()
    for forbidden in (
        root / "annotations" / "curated",
        root / "annotations",
        root / "data" / "original",
        root / "annotations" / "proposed" / "..",  # resolves to annotations/
    ):
        with pytest.raises(ExportRefused, match="not exactly"):
            write_records(built.records, forbidden, root)
    assert sorted((dataspace / "annotations" / "curated").iterdir()) == []
    assert not any(dataspace.rglob("synthfam-*.json"))


def test_a_missing_proposed_folder_is_not_created(export, dataspace, assessed):
    shutil.rmtree(dataspace / "annotations" / "proposed")
    with pytest.raises(ExportRefused, match="refusing to create"):
        export()
    assert not (dataspace / "annotations" / "proposed").exists()


def test_a_symlinked_proposed_folder_is_refused(export, dataspace, tmp_path, assessed):
    shutil.rmtree(dataspace / "annotations" / "proposed")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        os.symlink(elsewhere, dataspace / "annotations" / "proposed", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create symlinks without privileges")
    with pytest.raises(ExportRefused, match="not exactly"):
        export()
    assert list(elsewhere.iterdir()) == []


# -- overwrite -------------------------------------------------------------------------------


def test_an_existing_file_is_never_overwritten_without_the_flag(export, dataspace, assessed):
    export()
    target = dataspace / "annotations" / "proposed" / "synthfam-original-F1-pid-metadata.json"
    target.write_text("SENTINEL", encoding="utf-8")
    with pytest.raises(ExportRefused, match="already exist"):
        export()
    assert target.read_text(encoding="utf-8") == "SENTINEL"


def test_a_conflict_on_one_file_writes_none_of_the_others(export, dataspace, assessed):
    proposed = dataspace / "annotations" / "proposed"
    (proposed / "synthfam-original-F2-metadata-present.json").write_text("x", encoding="utf-8")
    with pytest.raises(ExportRefused):
        export()
    assert [p.name for p in proposed.iterdir()] == ["synthfam-original-F2-metadata-present.json"]


def test_overwrite_replaces_an_untouched_draft(export, dataspace, assessed):
    export()
    proposed = dataspace / "annotations" / "proposed"
    target = proposed / "synthfam-original-F1-pid-metadata.json"
    record = json.loads(target.read_text(encoding="utf-8"))
    record["judgement"]["rationale"] = "stale"
    target.write_text(json.dumps(record), encoding="utf-8")
    export(overwrite=True)
    assert json.loads(target.read_text(encoding="utf-8"))["judgement"]["rationale"] != "stale"


@pytest.mark.parametrize("status", ["in_review", "rejected"])
def test_overwrite_never_clobbers_a_record_a_reviewer_moved_past_draft(
    export, dataspace, assessed, status
):
    export()
    target = dataspace / "annotations" / "proposed" / "synthfam-original-F1-pid-metadata.json"
    record = json.loads(target.read_text(encoding="utf-8"))
    record["review"] = {"status": status, "comments": "human notes"}
    reviewed = json.dumps(record)
    target.write_text(reviewed, encoding="utf-8")
    with pytest.raises(ExportRefused, match="not an untouched draft"):
        export(overwrite=True)
    assert target.read_text(encoding="utf-8") == reviewed


def test_overwrite_never_replaces_a_human_authored_record(export, dataspace, assessed):
    export()
    target = dataspace / "annotations" / "proposed" / "synthfam-original-F1-pid-metadata.json"
    record = json.loads(target.read_text(encoding="utf-8"))
    record["provenance"]["method"] = "expert_review"
    target.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ExportRefused, match="not an untouched draft"):
        export(overwrite=True)


def test_overwrite_removes_drafts_the_export_no_longer_produces(
    export, dataspace, assessed, write_assessment
):
    export()
    proposed = dataspace / "annotations" / "proposed"
    stale = proposed / "synthfam-original-F2-metadata-present.json"
    assert stale.exists()
    write_assessment("fair", [result("F1-PID-METADATA", "F1", "pass")])
    summary = export(overwrite=True)
    assert not stale.exists()
    assert summary["removed"] == [stale.name]
    assert sorted(p.name for p in proposed.iterdir()) == ["synthfam-original-F1-pid-metadata.json"]


def test_overwrite_keeps_obsolete_records_that_are_not_untouched_data2agent_drafts(
    export, dataspace, assessed, write_assessment
):
    export()
    proposed = dataspace / "annotations" / "proposed"
    reviewed = proposed / "synthfam-original-F2-metadata-present.json"
    record = json.loads(reviewed.read_text(encoding="utf-8"))
    record["review"] = {"status": "in_review"}
    reviewed.write_text(json.dumps(record), encoding="utf-8")
    # Another harness's draft for the same dataset, and a different dataset version's.
    other = dict(record, review={"status": "draft"})
    other["provenance"] = dict(record["provenance"], notes="written by another harness")
    (proposed / "synthfam-original-other.json").write_text(json.dumps(other), encoding="utf-8")
    old = dict(record, review={"status": "draft"})
    old["dataset"] = dict(record["dataset"], version="sha256:" + "0" * 64)
    old["provenance"] = dict(
        record["provenance"], notes=f"Data2Agent dataset_id {old['dataset']['version']}."
    )
    (proposed / "synthfam-original-old.json").write_text(json.dumps(old), encoding="utf-8")

    write_assessment("fair", [result("F1-PID-METADATA", "F1", "pass")])
    summary = export(overwrite=True)
    assert summary["removed"] == []
    assert sorted(p.name for p in proposed.iterdir()) == [
        "synthfam-original-F1-pid-metadata.json",
        "synthfam-original-F2-metadata-present.json",
        "synthfam-original-old.json",
        "synthfam-original-other.json",
    ]


def test_without_overwrite_obsolete_drafts_are_left_alone(export, dataspace, assessed):
    export()
    proposed = dataspace / "annotations" / "proposed"
    for path in proposed.iterdir():
        path.rename(path.with_name("kept-" + path.name))
    summary = export()
    assert summary["removed"] == []
    assert len(list(proposed.iterdir())) == 4


def test_overwrite_refuses_an_unreadable_existing_file(export, dataspace, assessed):
    export()
    target = dataspace / "annotations" / "proposed" / "synthfam-original-F1-pid-metadata.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(ExportRefused, match="not an untouched draft"):
        export(overwrite=True)


# -- invariants ---------------------------------------------------------------------------------


def _one_record(ingest_dir):
    return build_annotations(
        ingest_dir,
        family=FAMILY,
        variant="original",
        dataset_path=DATASET_PATH,
        agent_model=AGENT_MODEL,
        created_at=CREATED_AT,
    ).records[0]


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda r: r["review"].update(status="approved"), "review.status"),
        (lambda r: r["review"].update(status="in_review"), "review.status"),
        (lambda r: r["review"].update(status="deprecated"), "review.status"),
        (lambda r: r["review"].update(reviewed_by="someone"), "reviewed_by"),
        (lambda r: r["provenance"].update(method="expert_review"), "provenance.method"),
        (lambda r: r["provenance"].update(agent_model=""), "agent_model"),
        (lambda r: r["provenance"].pop("agent_model"), "agent_model"),
        (lambda r: r["provenance"].update(created_at="yesterday"), "created_at"),
        (lambda r: r.update(evidence=[]), "evidence"),
        (lambda r: r["evidence"][0].update(locator="/abs"), "locator"),
        (lambda r: r["evidence"][0].update(locator="a/../b"), "locator"),
        (lambda r: r["evidence"][0].update(locator="has space"), "locator"),
        (
            lambda r: r["evidence"][0].update(checksum={"algorithm": "md5", "value": "0"}),
            "checksum",
        ),
        (lambda r: r.pop("fair_dimension"), "fair_dimension"),
        (lambda r: r["judgement"].update(label="approved"), "label"),
        (lambda r: r["judgement"].update(rationale=" "), "rationale"),
        (lambda r: r["target"].update(path="/etc"), "target"),
        (lambda r: r.update(annotation_id="bad id"), "annotation_id"),
        (lambda r: r["criterion"].update(description=""), "criterion"),
    ],
)
def test_a_record_that_breaks_an_invariant_is_a_hard_internal_error(
    ingest_dir, assessed, edit, message
):
    record = json.loads(json.dumps(_one_record(ingest_dir)))
    assert_proposal_invariants(record)  # the untouched record is fine
    edit(record)
    with pytest.raises(ExportInternalError, match=message):
        assert_proposal_invariants(record)


def test_a_non_draft_record_never_reaches_the_disk(export, dataspace, assessed, monkeypatch):
    from data2agent.export import annotations

    real = annotations.build_annotations

    def tampered(*args, **kwargs):
        built = real(*args, **kwargs)
        built.records[0]["review"]["status"] = "approved"
        return built

    monkeypatch.setattr(annotations, "build_annotations", tampered)
    with pytest.raises(ExportInternalError, match="review.status"):
        export()
    assert read_proposed(dataspace) == {}
    assert sorted((dataspace / "annotations" / "curated").iterdir()) == []


def test_write_records_rechecks_the_invariants_itself(dataspace, ingest_dir, assessed):
    record = json.loads(json.dumps(_one_record(ingest_dir)))
    record["review"]["status"] = "approved"
    with pytest.raises(ExportInternalError):
        write_records([record], dataspace / "annotations" / "proposed", dataspace)
    assert read_proposed(dataspace) == {}


def test_no_temporary_files_are_left_behind(export, dataspace, assessed):
    export()
    names = [p.name for p in (dataspace / "annotations" / "proposed").iterdir()]
    assert names and all(name.endswith(".json") and not name.startswith(".") for name in names)


def test_a_failed_write_leaves_no_partial_output(dataspace, ingest_dir, assessed, monkeypatch):
    built = build_annotations(
        ingest_dir,
        family=FAMILY,
        variant="original",
        dataset_path=DATASET_PATH,
        agent_model=AGENT_MODEL,
        created_at=CREATED_AT,
    )
    from data2agent.export import annotations

    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(annotations.os, "replace", flaky)
    with pytest.raises(OSError):
        write_records(built.records, dataspace / "annotations" / "proposed", dataspace)
    leftovers = [p.name for p in (dataspace / "annotations" / "proposed").iterdir()]
    # Staging files are cleaned up and the file already renamed is rolled back.
    assert leftovers == []


def test_a_failed_overwrite_restores_the_previous_drafts(
    export, dataspace, ingest_dir, assessed, monkeypatch
):
    export()
    proposed = dataspace / "annotations" / "proposed"
    before = {p.name: p.read_bytes() for p in proposed.iterdir()}
    built = build_annotations(
        ingest_dir,
        family=FAMILY,
        variant="original",
        dataset_path=DATASET_PATH,
        agent_model="a-different-model",
        created_at=CREATED_AT,
    )
    from data2agent.export import annotations

    calls = {"n": 0}
    real_replace = os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(annotations.os, "replace", flaky)
    with pytest.raises(OSError):
        write_records(built.records, proposed, dataspace, overwrite=True)
    assert {p.name: p.read_bytes() for p in proposed.iterdir()} == before


# -- CLI ---------------------------------------------------------------------------


def _cli(ingest_dir, dataspace, *extra):
    return main(
        [
            "export-annotations",
            str(ingest_dir),
            "--dataspace",
            str(dataspace),
            "--dataset-path",
            DATASET_PATH,
            "--family",
            FAMILY,
            "--variant",
            "original",
            "--agent-model",
            AGENT_MODEL,
            "--created-at",
            CREATED_AT,
            *extra,
        ]
    )


def test_the_cli_prints_a_machine_readable_summary(ingest_dir, dataspace, assessed, capsys):
    assert _cli(ingest_dir, dataspace) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["written"] == 2
    assert summary["schema_validated"] in (True, False)
    assert summary["output_dir"] == "annotations/proposed"
    assert len(read_proposed(dataspace)) == 2


def test_the_cli_warns_visibly_when_it_could_not_validate(ingest_dir, dataspace, assessed, capsys):
    (dataspace / "schemas" / "annotation.schema.json").unlink()
    assert _cli(ingest_dir, dataspace) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["schema_validated"] is False
    assert "warning: records were NOT validated" in captured.err


def test_the_cli_refuses_with_exit_code_2_and_writes_nothing(
    ingest_dir, dataspace, assessed, capsys
):
    _cli(ingest_dir, dataspace)  # first run creates the files
    capsys.readouterr()
    assert _cli(ingest_dir, dataspace) == 2
    assert "already exist" in capsys.readouterr().err
    assert _cli(ingest_dir, dataspace, "--overwrite") == 0


def test_the_cli_refuses_a_traversing_dataset_path(ingest_dir, dataspace, assessed, capsys):
    code = main(
        [
            "export-annotations",
            str(ingest_dir),
            "--dataspace",
            str(dataspace),
            "--dataset-path",
            "data/../..",
            "--family",
            FAMILY,
            "--variant",
            "original",
            "--agent-model",
            AGENT_MODEL,
        ]
    )
    assert code == 2
    assert "'..'" in capsys.readouterr().err
    assert read_proposed(dataspace) == {}


def test_the_cli_requires_the_agent_model(ingest_dir, dataspace, assessed, capsys):
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "export-annotations",
                str(ingest_dir),
                "--dataspace",
                str(dataspace),
                "--dataset-path",
                DATASET_PATH,
                "--family",
                FAMILY,
                "--variant",
                "original",
            ]
        )
    assert raised.value.code == 2
    assert "--agent-model" in capsys.readouterr().err
    assert read_proposed(dataspace) == {}


def test_the_cli_rejects_a_blank_agent_model(ingest_dir, dataspace, assessed, capsys):
    assert _cli(ingest_dir, dataspace, "--agent-model", "  ") == 2
    assert "agent_model is required" in capsys.readouterr().err


def test_the_cli_rejects_an_unknown_variant(ingest_dir, dataspace, assessed):
    with pytest.raises(SystemExit):
        _cli(ingest_dir, dataspace, "--variant", "approved")


# -- layering ---------------------------------------------------------------------


SRC = Path(__file__).resolve().parents[2] / "src" / "data2agent"


def _imports_export(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            parts = module.split(".")
            if node.level and parts[0] == "export":
                return True
            if not node.level and parts[:2] == ["data2agent", "export"]:
                return True
            if node.level == 1 and not module and any(a.name == "export" for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name.startswith("data2agent.export") for a in node.names):
                return True
    return False


def test_nothing_but_the_cli_entry_point_imports_the_export_package():
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in sorted(SRC.rglob("*.py"))
        if path.relative_to(SRC).parts[0] != "export"
        and path.name != "cli.py"
        and _imports_export(path)
    ]
    assert not offenders, f"layering violation: {offenders} import data2agent.export"


def test_the_cli_imports_export_only_lazily():
    tree = ast.parse((SRC / "cli.py").read_text(encoding="utf-8"))
    top_level = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "export"
    ]
    assert not top_level, "cli.py must import data2agent.export inside the handler"


def test_the_reference_reader_is_not_re_exported_and_not_reachable_from_mcp():
    import data2agent.export as package

    assert not hasattr(package, "read_reference_labels")
    for path in sorted((SRC / "mcp").rglob("*.py")):
        assert "reference_labels" not in path.read_text(encoding="utf-8"), path


def test_importing_the_mcp_layer_does_not_import_the_export_package():
    import subprocess

    code = (
        "import sys, data2agent.mcp.service; "
        "sys.exit(1 if any(m.startswith('data2agent.export') for m in sys.modules) else 0)"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC.parent))
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 0


def test_the_export_core_needs_no_third_party_import_at_module_level():
    stdlib = set(sys.stdlib_module_names)
    for path in sorted((SRC / "export").rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            assert names <= stdlib | {"data2agent"}, (path.name, names)
