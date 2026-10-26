"""The reference-label reader used by scorers, and the optional real-dataspace check."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from export_helpers import AGENT_MODEL, CREATED_AT, FAMILY, read_proposed, result

from data2agent.export.reference_labels import ReferenceLabelError, read_reference_labels


def _record(
    criterion: str,
    label: str,
    *,
    status: str = "approved",
    method: str = "expert_review",
    family: str = FAMILY,
    variant: str = "original",
) -> dict:
    review = {"status": status}
    if status == "approved":
        review.update(reviewed_by="reviewer", reviewed_at="2026-01-01T00:00:00Z")
    return {
        "schema_version": "0.1.0",
        "annotation_id": f"{family}-{variant}-{criterion}",
        "dataset": {"dataset_family_id": family, "variant": variant},
        "annotation_type": "fair_assessment",
        "fair_dimension": "findable",
        "target": {"kind": "dataset", "path": "data/original/x"},
        "criterion": {"id": criterion, "description": "d"},
        "judgement": {"label": label, "rationale": "r"},
        "evidence": [{"locator": "x", "description": "d"}],
        "provenance": {"method": method, "created_by": "human", "created_at": CREATED_AT},
        "review": review,
    }


def _put(dataspace: Path, folder: str, name: str, record: dict) -> None:
    target = dataspace / "annotations" / folder
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")


def test_only_approved_curated_records_of_the_family_and_variant_are_returned(dataspace):
    _put(dataspace, "curated", "a", _record("F1", "conformant"))
    _put(dataspace, "curated", "b", _record("F2", "non_conformant"))
    _put(dataspace, "curated", "c", _record("F3", "partial"))
    _put(dataspace, "curated", "d", _record("F4", "insufficient_evidence"))
    _put(dataspace, "curated", "e", _record("F5", "not_applicable"))
    # ignored: deprecated, other family, other variant
    _put(dataspace, "curated", "dep", _record("F6", "conformant", status="deprecated"))
    _put(dataspace, "curated", "fam", _record("F7", "conformant", family="other"))
    _put(dataspace, "curated", "var", _record("F8", "conformant", variant="curated"))
    assert read_reference_labels(dataspace, FAMILY, "original") == {
        "F1": "conformant",
        "F2": "non_conformant",
        "F3": "partial",
        "F4": "insufficient_evidence",
        "F5": "not_applicable",
    }
    assert read_reference_labels(dataspace, FAMILY, "curated") == {"F8": "conformant"}


def test_nothing_under_proposed_is_ever_a_reference(dataspace):
    _put(dataspace, "proposed", "p1", _record("F1", "conformant", status="approved"))
    _put(
        dataspace,
        "proposed",
        "p2",
        _record("F2", "conformant", status="draft", method="agent_proposal"),
    )
    _put(dataspace, "curated", "ok", _record("F3", "non_conformant"))
    assert read_reference_labels(dataspace, FAMILY, "original") == {"F3": "non_conformant"}


def test_an_exported_proposal_is_never_returned_as_a_reference(export, dataspace, write_assessment):
    write_assessment("fair", [result("F1-PID-METADATA", "F1", "pass")])
    export()
    assert read_proposed(dataspace)
    assert read_reference_labels(dataspace, FAMILY, "original") == {}


def test_two_approved_records_that_disagree_raise(dataspace):
    _put(dataspace, "curated", "a", _record("F1", "conformant"))
    _put(dataspace, "curated", "b", _record("F1", "non_conformant"))
    with pytest.raises(ReferenceLabelError, match="disagree on criterion 'F1'"):
        read_reference_labels(dataspace, FAMILY, "original")


def test_two_approved_records_that_agree_do_not_raise(dataspace):
    _put(dataspace, "curated", "a", _record("F1", "conformant"))
    _put(dataspace, "curated", "b", _record("F1", "conformant"))
    assert read_reference_labels(dataspace, FAMILY, "original") == {"F1": "conformant"}


def test_a_deprecated_record_does_not_conflict_with_its_approved_correction(dataspace):
    _put(dataspace, "curated", "old", _record("F1", "conformant", status="deprecated"))
    _put(dataspace, "curated", "new", _record("F1", "non_conformant"))
    assert read_reference_labels(dataspace, FAMILY, "original") == {"F1": "non_conformant"}


def test_an_approved_agent_proposal_is_refused_outright(dataspace):
    _put(dataspace, "curated", "bad", _record("F1", "conformant", method="agent_proposal"))
    with pytest.raises(ReferenceLabelError, match="approved agent_proposal"):
        read_reference_labels(dataspace, FAMILY, "original")


def test_a_corrupt_curated_file_raises_rather_than_thinning_the_answer_key(dataspace):
    _put(dataspace, "curated", "ok", _record("F1", "conformant"))
    (dataspace / "annotations" / "curated" / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(ReferenceLabelError, match="cannot be read"):
        read_reference_labels(dataspace, FAMILY, "original")


def test_an_approved_record_with_an_invalid_label_raises(dataspace):
    _put(dataspace, "curated", "bad", _record("F1", "approved"))
    with pytest.raises(ReferenceLabelError, match="valid judgement label"):
        read_reference_labels(dataspace, FAMILY, "original")


def test_non_json_files_are_ignored_and_an_empty_folder_gives_nothing(dataspace):
    (dataspace / "annotations" / "curated" / ".gitkeep").write_text("", encoding="utf-8")
    assert read_reference_labels(dataspace, FAMILY, "original") == {}


def test_a_missing_curated_folder_is_an_error(dataspace):
    shutil.rmtree(dataspace / "annotations" / "curated")
    with pytest.raises(ReferenceLabelError, match="does not exist"):
        read_reference_labels(dataspace, FAMILY, "original")


def test_the_module_states_its_holdout_warning():
    from data2agent.export import reference_labels

    text = (reference_labels.__doc__ or "") + (reference_labels.read_reference_labels.__doc__ or "")
    assert "NEVER" in text and "holdout" in text.lower() and "SCORING TOOLS ONLY" in text


# -- optional: the real dataspace (a TEMP COPY of it, never the original) ------------


@pytest.mark.skipif(
    not os.environ.get("D2A_DATASPACE_ROOT"), reason="D2A_DATASPACE_ROOT is not set"
)
def test_against_a_temp_copy_of_the_real_dataspace(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from data2agent.export import export_annotations
    from data2agent.ingest import ingest

    real = Path(os.environ["D2A_DATASPACE_ROOT"])
    assert (real / "schemas" / "annotation.schema.json").is_file(), "not a FAIR-VCG dataspace"
    copy = tmp_path / "dataspace-copy"
    shutil.copytree(real, copy, ignore=shutil.ignore_patterns(".git"))

    family = "d2a-export-smoke"
    dataset_rel = f"data/original/{family}"
    dataset = copy / dataset_rel
    dataset.mkdir(parents=True)
    (dataset / "t.csv").write_text("id,v\n1,2\n2,3\n", encoding="utf-8")
    out = tmp_path / "ingest-out"
    ingest(dataset, out)

    from data2agent.cli import main

    assert main(["assess", str(out)]) == 0
    summary = export_annotations(
        out,
        dataspace=copy,
        dataset_path=dataset_rel,
        family=family,
        variant="original",
        agent_model=AGENT_MODEL,
        created_at=CREATED_AT,
    )
    assert summary["schema_validated"] is True
    assert summary["written"] == 12

    schema = json.loads((copy / "schemas" / "annotation.schema.json").read_text("utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    files = sorted((copy / "annotations" / "proposed").glob(f"{family}-*.json"))
    assert len(files) == 12
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        assert not list(validator.iter_errors(record)), path.name
        assert record["review"] == {"status": "draft"}
        assert record["provenance"]["method"] == "agent_proposal"

    # the reader never sees proposals, whatever the real curated folder holds
    labels = read_reference_labels(copy, family, "original")
    assert labels == {}
