"""Mapping, evidence, determinism and validation of the annotation exporter."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
from export_helpers import (
    AGENT_MODEL,
    CREATED_AT,
    DATASET_PATH,
    FAMILY,
    LOCATOR,
    read_proposed,
    result,
    write_profile,
)

from data2agent.export import (
    AnnotationExportError,
    build_annotations,
    export_annotations,
    label_for_result,
    result_for_label,
)
from data2agent.export.annotations import (
    EXPORT_SPELLING,
    LABEL_BY_RESULT,
    RESULT_BY_LABEL,
    normalise_created_at,
    shared_ids,
)

pytest.importorskip("yaml")


def _ann_id(tail: str, variant: str = "original", suffix: str = "") -> str:
    """``<family>-<variant>-<tail>``, the collaborator's format: no ``ann-`` prefix."""
    return f"{FAMILY}-{variant}-{tail}{suffix}"


# -- label mapping --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "label"),
    [
        ("pass", "conformant"),
        ("fail", "non_conformant"),
        ("partial", "partial"),
        ("unknown", "insufficient_evidence"),
        ("not_applicable", "not_applicable"),
    ],
)
def test_every_mapping_row_reaches_the_record(export, dataspace, write_assessment, outcome, label):
    assert label_for_result(outcome) == label
    text = {"rationale": "because the check said so"} if outcome != "pass" else {}
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", outcome, **text)])
    export()
    record = read_proposed(dataspace)[_ann_id("F2-metadata-present")]
    assert record["judgement"]["label"] == label
    assert record["annotation_type"] == "fair_assessment"


def test_the_mapping_tables_are_exactly_the_five_rows():
    assert LABEL_BY_RESULT == {
        "pass": "conformant",
        "fail": "non_conformant",
        "partial": "partial",
        "unknown": "insufficient_evidence",
        "not_applicable": "not_applicable",
    }
    assert set(RESULT_BY_LABEL) == set(LABEL_BY_RESULT.values())


def test_an_unknown_result_word_is_refused_not_guessed(export, write_assessment):
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "maybe")])
    with pytest.raises(AnnotationExportError, match="cannot map result 'maybe'"):
        export()


def test_the_reverse_mapping_handles_partial_and_insufficient_evidence_explicitly():
    assert result_for_label("conformant") == "pass"
    assert result_for_label("non_conformant") == "fail"
    assert result_for_label("not_applicable") == "not_applicable"
    # Not folded into 'fail': an unresolved criterion stays unresolved.
    assert result_for_label("insufficient_evidence") == "unknown"
    # No Data2Agent equivalent, so it stays itself unless the scorer decides otherwise.
    assert result_for_label("partial") == "partial"
    assert result_for_label("partial", partial_as="fail") == "fail"
    assert result_for_label("conformant", partial_as="fail") == "pass"
    with pytest.raises(AnnotationExportError, match="partial_as"):
        result_for_label("partial", partial_as="conformant")
    with pytest.raises(AnnotationExportError, match="unknown dataspace label"):
        result_for_label("approved")


# -- FAIR dimensions ------------------------------------------------------------------


def test_the_fair_dimension_comes_from_the_principle_letter(export, dataspace, write_assessment):
    write_assessment(
        "fair",
        [
            result("F1-PID-METADATA", "F1", "pass"),
            result("A1-RETRIEVAL-PROTOCOL", "A1", "unknown", rationale="cannot tell locally"),
            result("I1-DATA-FORMATS-OPEN", "I1", "pass"),
            result("R1.1-LICENCE-DECLARED", "R1.1", "fail", rationale="no licence"),
        ],
    )
    export()
    records = read_proposed(dataspace)
    dimensions = {r["criterion"]["id"]: r["fair_dimension"] for r in records.values()}
    assert dimensions == {
        "FAIR-F1_PID-METADATA": "findable",
        "FAIR-A1_RETRIEVAL-PROTOCOL": "accessible",
        "FAIR-I1_DATA-FORMATS-OPEN": "interoperable",
        "FAIR-R1.1_LICENSE-DECLARED": "reusable",
    }


def test_the_real_fair_assessment_exports_every_rule(export, dataspace, ingest_dir):
    from data2agent.cli import main

    assert main(["assess", str(ingest_dir)]) == 0
    summary = export()
    records = read_proposed(dataspace)
    assert summary["written"] == len(records) == 12
    assert {r["annotation_type"] for r in records.values()} == {"fair_assessment"}
    for record in records.values():
        assert record["criterion"]["description"].strip()
        assert record["judgement"]["rationale"].strip()


def test_the_criterion_description_is_the_profile_question(export, dataspace, write_assessment):
    write_assessment("fair", [result("F1-PID-METADATA", "F1", "pass")])
    export()
    record = read_proposed(dataspace)[_ann_id("F1-pid-metadata")]
    assert record["criterion"] == {
        "id": "FAIR-F1_PID-METADATA",
        "description": "Does the dataset's metadata carry a globally unique persistent identifier?",
    }


def test_a_principle_that_is_neither_fair_nor_quality_is_skipped_with_a_warning(
    export, dataspace, write_assessment, tmp_path
):
    root = write_profile(
        tmp_path / "profiles",
        "arrive",
        {
            "A-REPORT": ("Reporting", "Is the study reported?"),
            "A-REPRO": ("Reproducibility", "Is it reproducible?"),
            "A-FINDABLE": ("F1", "Is it findable?"),
        },
    )
    write_assessment(
        "arrive",
        [
            result("A-REPORT", "Reporting", "pass"),
            result("A-REPRO", "Reproducibility", "fail", rationale="no"),
            result("A-FINDABLE", "F1", "pass"),
        ],
    )
    summary = export(profiles_root=root)
    assert list(read_proposed(dataspace)) == [_ann_id("F1-a-findable")]
    assert [s["rule_id"] for s in summary["skipped"]] == ["A-REPORT", "A-REPRO"]
    assert sum("maps to no FAIR dimension" in w for w in summary["warnings"]) == 2
    # 'Reproducibility' merely starts with R: it is not guessed into 'reusable'.
    assert "a-repro" not in " ".join(read_proposed(dataspace)).lower()


# -- quality profile ------------------------------------------------------------------


@pytest.fixture
def quality_root(tmp_path: Path) -> Path:
    return write_profile(
        tmp_path / "profiles",
        "quality",
        {
            "Q-PASS": ("typing", "Are values typed?"),
            "Q-FAIL": ("typing", "Are headers portable?"),
            "Q-PARTIAL": ("typing", "Is encoding clean?"),
            "Q-UNKNOWN": ("typing", "Are placeholders absent?"),
            "Q-NA": ("typing", "Is a column redundant?"),
        },
    )


def test_quality_issue_is_written_only_for_fail_partial_unknown(
    export, dataspace, write_assessment, quality_root
):
    write_assessment(
        "quality",
        [
            result("Q-PASS", "typing", "pass"),
            result("Q-FAIL", "typing", "fail", rationale="bad headers"),
            result("Q-PARTIAL", "typing", "partial", rationale="some bad"),
            result("Q-UNKNOWN", "typing", "unknown", rationale="cannot tell"),
            result("Q-NA", "typing", "not_applicable", rationale="no such column"),
        ],
    )
    summary = export(profiles_root=quality_root)
    records = read_proposed(dataspace)
    assert sorted(r["criterion"]["id"] for r in records.values()) == [
        "QC_FAIL",
        "QC_PARTIAL",
        "QC_UNKNOWN",
    ]
    for record in records.values():
        assert record["annotation_type"] == "quality_issue"
        assert "fair_dimension" not in record
    skipped = {s["rule_id"]: s["reason"] for s in summary["skipped"]}
    assert set(skipped) == {"Q-PASS", "Q-NA"}
    assert "issues only" in skipped["Q-PASS"]


def test_each_quality_offender_gets_its_own_annotation(
    export, dataspace, write_assessment, quality_root
):
    write_assessment(
        "quality",
        [
            result(
                "Q-FAIL",
                "typing",
                "fail",
                rationale="padded label text",
                observations={
                    "offenders": [
                        {"file": "t.csv", "field": "Trial"},
                        {"file": "t.csv", "field": "Trial"},  # duplicate, dropped
                        "v",  # a bare field name
                        {"file": "README.md"},  # a whole file
                        "t.csv",  # a bare file
                        {"file": "not-in-manifest.csv", "field": "x"},  # unreadable, skipped
                        42,  # unreadable, skipped
                    ]
                },
            )
        ],
    )
    summary = export(profiles_root=quality_root)
    records = read_proposed(dataspace)
    assert sorted(records) == [_ann_id("QC-fail", suffix=f"-{n}") for n in (1, 2, 3, 4)]
    targets = [records[_ann_id("QC-fail", suffix=f"-{n}")]["target"] for n in (1, 2, 3, 4)]
    assert targets[0] == {
        "kind": "field",
        "path": f"{DATASET_PATH}/t.csv",
        "field_name": "Trial",
    }
    assert targets[1]["kind"] == "field" and targets[1]["field_name"] == "v"
    assert targets[2] == {"kind": "file", "path": f"{DATASET_PATH}/README.md"}
    assert targets[3] == {"kind": "file", "path": f"{DATASET_PATH}/t.csv"}
    assert sum("skipped" in w for w in summary["warnings"]) == 2
    # The offender's own file leads the evidence, with its manifest checksum.
    first = records[_ann_id("QC-fail", suffix="-1")]["evidence"][0]
    assert first["locator"] == f"{DATASET_PATH}/t.csv"
    assert "offending field 'Trial'" in first["description"]


def test_a_quality_result_without_offenders_targets_the_dataset(
    export, dataspace, write_assessment, quality_root
):
    write_assessment("quality", [result("Q-FAIL", "typing", "fail", rationale="bad")])
    export(profiles_root=quality_root)
    record = read_proposed(dataspace)[_ann_id("QC-fail")]
    assert record["target"] == {"kind": "dataset", "path": DATASET_PATH}


def test_unreadable_offenders_fall_back_to_the_dataset_with_a_warning(
    export, dataspace, write_assessment, quality_root
):
    write_assessment(
        "quality",
        [result("Q-FAIL", "typing", "fail", rationale="bad", observations={"offenders": [7]})],
    )
    summary = export(profiles_root=quality_root)
    assert read_proposed(dataspace)[_ann_id("QC-fail")]["target"]["kind"] == "dataset"
    assert any("no offender could be read" in w for w in summary["warnings"])


def test_an_unshipped_profile_is_refused_not_described_by_guess(export, write_assessment):
    write_assessment("quality", [result("Q-FAIL", "typing", "fail", rationale="bad")])
    with pytest.raises(AnnotationExportError, match="never guessed"):
        export()  # no 'quality' profile ships with this checkout


def test_a_rule_the_profile_does_not_define_is_refused(export, write_assessment, quality_root):
    write_assessment("quality", [result("Q-GHOST", "typing", "fail", rationale="bad")])
    with pytest.raises(AnnotationExportError, match="does not define"):
        export(profiles_root=quality_root)


# -- rationale and provenance ---------------------------------------------------------


def test_a_pass_without_a_rationale_cites_its_evidence_checks(
    export, dataspace, write_assessment, claim_for
):
    write_assessment(
        "fair",
        [
            result(
                "I1-DATA-FORMATS-OPEN",
                "I1",
                "pass",
                evidence=[claim_for("t.csv", check="file.format-detection")],
            )
        ],
    )
    export()
    rationale = read_proposed(dataspace)[_ann_id("I1-data-formats-open")]["judgement"]["rationale"]
    assert "file.format-detection" in rationale
    assert rationale == (
        "Rule I1-DATA-FORMATS-OPEN returned 'pass' (conformant); the deterministic check "
        "cited these evidence checks: file.format-detection."
    )


def test_a_stated_rationale_is_kept_with_its_whitespace_normalised(
    export, dataspace, write_assessment
):
    write_assessment(
        "fair", [result("F2-METADATA-PRESENT", "F2", "fail", rationale="no  file\nwas found\n")]
    )
    export()
    record = read_proposed(dataspace)[_ann_id("F2-metadata-present")]
    assert record["judgement"]["rationale"] == "no file was found"


def test_provenance_is_a_draft_agent_proposal_with_the_given_model(
    export, dataspace, write_assessment, manifest
):
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    export()
    record = read_proposed(dataspace)[_ann_id("F2-metadata-present")]
    from data2agent import __version__

    assert record["schema_version"] == "0.1.0"
    assert record["dataset"] == {
        "dataset_family_id": FAMILY,
        "variant": "original",
        "version": manifest["dataset_id"],
    }
    provenance = record["provenance"]
    assert provenance["method"] == "agent_proposal"
    assert provenance["created_by"] == f"data2agent {__version__}"
    assert provenance["created_at"] == CREATED_AT
    assert provenance["agent_model"] == AGENT_MODEL
    assert "computed by code" in provenance["notes"]
    assert "profile fair 0.1.0" in provenance["notes"]
    assert "F2-METADATA-PRESENT" in provenance["notes"]
    assert record["review"] == {"status": "draft"}


def test_created_by_can_be_overridden(export, dataspace, write_assessment):
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    export(created_by="pipeline run 7")
    record = read_proposed(dataspace)[_ann_id("F2-metadata-present")]
    assert record["provenance"]["created_by"] == "pipeline run 7"


@pytest.mark.parametrize("model", [None, "", "   "])
def test_agent_model_is_required_and_never_defaulted(export, dataspace, write_assessment, model):
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    with pytest.raises(AnnotationExportError, match="agent_model is required"):
        export(agent_model=model)
    assert read_proposed(dataspace) == {}


def test_created_at_must_be_utc_iso8601_and_defaults_to_now():
    assert normalise_created_at("2026-01-02T03:04:05Z") == "2026-01-02T03:04:05Z"
    for bad in (
        "2026-01-02",
        "2026-01-02T03:04:05",
        "2026-01-02T03:04:05+01:00",
        "2026-13-02T03:04:05Z",
    ):
        with pytest.raises(AnnotationExportError, match="created-at"):
            normalise_created_at(bad)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", normalise_created_at(None))


# -- evidence -------------------------------------------------------------------------


def test_evidence_locators_are_legal_relative_and_pinned_to_manifest_checksums(
    export, dataspace, write_assessment, claim_for, manifest
):
    write_assessment(
        "fair",
        [
            result(
                "I1-DATA-FORMATS-OPEN",
                "I1",
                "pass",
                evidence=[claim_for("t.csv"), claim_for("README.md")],
            ),
            result(
                "F3-METADATA-LINKS-DATA",
                "F3",
                "pass",
                evidence=[
                    {
                        "check": "metadata.file-convention",
                        "result": ["README.md"],
                        "detail": "linked",
                    }
                ],
            ),
            result("A1-RETRIEVAL-PROTOCOL", "A1", "unknown", rationale="offline"),
        ],
    )
    export()
    sha = {f["path"]: f["sha256"] for f in manifest["files"]}
    for record in read_proposed(dataspace).values():
        assert record["evidence"], record["annotation_id"]
        for item in record["evidence"]:
            assert re.fullmatch(LOCATOR, item["locator"])
            assert item["locator"].startswith(DATASET_PATH)
            assert item["description"].strip()
            if "checksum" in item:
                name = item["locator"][len(DATASET_PATH) + 1 :]
                assert item["checksum"] == {"algorithm": "sha256", "value": sha[name]}
                # ... and that really is the sha256 of the bytes in the dataspace.
                actual = hashlib.sha256((dataspace / item["locator"]).read_bytes()).hexdigest()
                assert actual == item["checksum"]["value"]
    record = read_proposed(dataspace)[_ann_id("I1-data-formats-open")]
    assert [e["locator"] for e in record["evidence"]] == [
        f"{DATASET_PATH}/t.csv",
        f"{DATASET_PATH}/README.md",
    ]
    assert claim_for("t.csv") in record["evidence"][0]["description"]


def test_a_result_that_cites_no_file_points_at_the_dataset_directory(
    export, dataspace, write_assessment
):
    write_assessment(
        "fair", [result("A1-RETRIEVAL-PROTOCOL", "A1", "unknown", rationale="offline")]
    )
    export()
    (item,) = read_proposed(dataspace)[_ann_id("A1-retrieval-protocol")]["evidence"]
    assert item["locator"] == DATASET_PATH
    assert "checksum" not in item
    assert "no single file is cited" in item["description"]


def test_a_file_name_that_cannot_be_a_locator_falls_back_to_the_dataset_directory(
    dataspace, tmp_path, write_assessment
):
    from data2agent.ingest import ingest

    (dataspace / DATASET_PATH / "my file.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    out = tmp_path / "ingest-spaces"
    ingest(dataspace / DATASET_PATH, out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    (digest,) = [f["sha256"] for f in manifest["files"] if f["path"] == "my file.csv"]
    (out / "assessment.json").write_text(
        json.dumps(
            {
                "assessment_version": "0.1.0",
                "dataset_id": manifest["dataset_id"],
                "profile": {"id": "fair", "version": "0.1.0"},
                "generator": {"mode": "fair-deterministic"},
                "results": [
                    result(
                        "I1-DATA-FORMATS-OPEN",
                        "I1",
                        "pass",
                        evidence=[{"check": "file.format-detection", "result": ["my file.csv"]}],
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    export_annotations(
        out,
        dataspace=dataspace,
        dataset_path=DATASET_PATH,
        family=FAMILY,
        variant="original",
        agent_model=AGENT_MODEL,
        created_at=CREATED_AT,
    )
    (item,) = read_proposed(dataspace)[_ann_id("I1-data-formats-open")]["evidence"]
    assert item["locator"] == DATASET_PATH
    assert "my file.csv" in item["description"] and digest in item["description"]
    assert "cannot be written as a legal dataspace locator" in item["description"]


def test_a_file_that_cannot_be_a_locator_must_still_exist_in_the_dataspace(dataspace, tmp_path):
    from data2agent.ingest import ingest

    odd = dataspace / DATASET_PATH / "my file.csv"
    odd.write_bytes(b"a,b\n1,2\n")
    out = tmp_path / "ingest-spaces"
    ingest(dataspace / DATASET_PATH, out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    (out / "assessment.json").write_text(
        json.dumps(
            {
                "assessment_version": "0.1.0",
                "dataset_id": manifest["dataset_id"],
                "profile": {"id": "fair", "version": "0.1.0"},
                "generator": {"mode": "fair-deterministic"},
                "results": [
                    result(
                        "I1-DATA-FORMATS-OPEN",
                        "I1",
                        "pass",
                        evidence=[{"check": "file.format-detection", "result": ["my file.csv"]}],
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    odd.unlink()
    with pytest.raises(AnnotationExportError, match="does not exist in the dataspace"):
        export_annotations(
            out,
            dataspace=dataspace,
            dataset_path=DATASET_PATH,
            family=FAMILY,
            variant="original",
            agent_model=AGENT_MODEL,
            created_at=CREATED_AT,
        )
    assert read_proposed(dataspace) == {}


def test_evidence_files_must_exist_in_the_dataspace(export, dataspace, write_assessment, claim_for):
    write_assessment(
        "fair", [result("I1-DATA-FORMATS-OPEN", "I1", "pass", evidence=[claim_for("t.csv")])]
    )
    (dataspace / DATASET_PATH / "t.csv").unlink()
    with pytest.raises(AnnotationExportError, match="does not exist in the dataspace"):
        export()
    assert read_proposed(dataspace) == {}


def test_a_size_mismatch_warns_but_pins_the_manifest_checksum(
    export, dataspace, write_assessment, claim_for
):
    write_assessment(
        "fair", [result("I1-DATA-FORMATS-OPEN", "I1", "pass", evidence=[claim_for("t.csv")])]
    )
    with (dataspace / DATASET_PATH / "t.csv").open("ab") as handle:
        handle.write(b"3,Trial   3,4.5\n")
    summary = export()
    assert any("ingest manifest" in w for w in summary["warnings"])


def test_an_assessment_for_another_dataset_is_refused(export, ingest_dir, write_assessment):
    path = write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["dataset_id"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AnnotationExportError, match="re-run 'assess'"):
        export()


def test_a_profile_file_whose_name_contradicts_its_profile_is_refused(
    export, ingest_dir, write_assessment
):
    path = write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    path.rename(ingest_dir / "assessment.quality.json")
    with pytest.raises(AnnotationExportError, match="contradicts its file name"):
        export()


def test_no_assessment_is_an_error(export):
    with pytest.raises(AnnotationExportError, match="no assessment"):
        export()


# -- multiple profiles -------------------------------------------------------------------


def test_fair_and_quality_together(export, dataspace, write_assessment, tmp_path):
    root = write_profile(
        tmp_path / "both", "quality", {"Q-FAIL": ("typing", "Are headers portable?")}
    )
    write_profile(
        root,
        "fair",
        {"F2-METADATA-PRESENT": ("F2", "Is metadata present?")},
    )
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    write_assessment("quality", [result("Q-FAIL", "typing", "fail", rationale="bad")])
    summary = export(profiles_root=root)
    assert summary["by_type"] == {"fair_assessment": 1, "quality_issue": 1}
    assert [p["id"] for p in summary["profiles"]] == ["fair", "quality"]


def test_a_colliding_annotation_id_is_an_error(export, write_assessment, tmp_path):
    # Two profiles grading the same FAIR principle under the same rule id.
    root = write_profile(tmp_path / "both", "arrive", {"F1-X": ("F1", "q?")})
    write_profile(root, "fair", {"F1-X": ("F1", "q?")})
    write_assessment("fair", [result("F1-X", "F1", "pass")])
    write_assessment("arrive", [result("F1-X", "F1", "fail", rationale="bad")])
    with pytest.raises(AnnotationExportError, match="collision"):
        export(profiles_root=root)


def test_the_same_rule_id_in_fair_and_quality_does_not_collide(
    export, dataspace, write_assessment, tmp_path
):
    """The criterion family (FAIR, QC) keeps them apart."""
    root = write_profile(tmp_path / "both", "quality", {"F1-X": ("typing", "q?")})
    write_profile(root, "fair", {"F1-X": ("F1", "q?")})
    write_assessment("fair", [result("F1-X", "F1", "pass")])
    write_assessment("quality", [result("F1-X", "typing", "fail", rationale="bad")])
    export(profiles_root=root)
    records = read_proposed(dataspace)
    assert {name: r["criterion"]["id"] for name, r in records.items()} == {
        _ann_id("F1-x"): "FAIR-F1_X",
        _ann_id("QC-f1-x"): "QC_F1-X",
    }


def test_a_rule_id_that_cannot_form_an_annotation_id_is_refused(export, write_assessment, tmp_path):
    root = write_profile(tmp_path / "p", "fair", {"F1 bad": ("F1", "q?")})
    write_assessment("fair", [result("F1 bad", "F1", "pass")])
    with pytest.raises(AnnotationExportError, match="legal annotation_id"):
        export(profiles_root=root)


# -- shared ids ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "rule_id", "principle", "criterion", "tail"),
    [
        # LICENCE is exported as LICENSE; nothing else about the slug changes.
        (
            "fair",
            "R1.1-LICENCE-DECLARED",
            "R1.1",
            "FAIR-R1.1_LICENSE-DECLARED",
            "R1.1-license-declared",
        ),
        # Split on the first hyphen only: the slug keeps its own hyphens.
        ("fair", "F1-PID-RESOLVABLE", "F1", "FAIR-F1_PID-RESOLVABLE", "F1-pid-resolvable"),
        # A dotted principle is one code, never split.
        (
            "fair",
            "R1.3-MISSING-VALUES-DECLARED",
            "R1.3",
            "FAIR-R1.3_MISSING-VALUES-DECLARED",
            "R1.3-missing-values-declared",
        ),
        # A rule id that does not start with its principle keeps every part of it.
        ("fair", "A-FINDABLE", "F1", "FAIR-F1_A-FINDABLE", "F1-a-findable"),
        # Quality rules have a category, not a code; only the Q- prefix goes.
        ("quality", "Q-HEADERS-PORTABLE", None, "QC_HEADERS-PORTABLE", "QC-headers-portable"),
        ("quality", "F1-X", None, "QC_F1-X", "QC-f1-x"),
        ("curation", "ORIGINAL-UNCHANGED", None, "CV_ORIGINAL-UNCHANGED", "CV-original-unchanged"),
        ("curation", "CELL-ROUND-TRIP", None, "CV_CELL-ROUND-TRIP", "CV-cell-round-trip"),
        ("curation", "PACKAGE-VALID", None, "CV_PACKAGE-VALID", "CV-package-valid"),
    ],
)
def test_shared_ids_follow_the_agreed_convention(source, rule_id, principle, criterion, tail):
    assert shared_ids(source, rule_id, principle) == (criterion, tail)


def test_the_spelling_map_replaces_whole_words_only():
    assert EXPORT_SPELLING == {"LICENCE": "LICENSE"}
    assert shared_ids("fair", "R1.1-LICENCES-X", "R1.1")[0] == "FAIR-R1.1_LICENCES-X"


def test_licence_is_exported_as_license_and_the_rule_id_stays_traceable(
    export, dataspace, write_assessment
):
    write_assessment("fair", [result("R1.1-LICENCE-DECLARED", "R1.1", "fail", rationale="none")])
    export()
    records = read_proposed(dataspace)
    assert list(records) == [f"{FAMILY}-original-R1.1-license-declared"]
    record = records[f"{FAMILY}-original-R1.1-license-declared"]
    assert record["annotation_id"] == f"{FAMILY}-original-R1.1-license-declared"
    assert record["criterion"]["id"] == "FAIR-R1.1_LICENSE-DECLARED"
    # The internal rule id, in its own spelling, is still in the record.
    assert "rule R1.1-LICENCE-DECLARED," in record["provenance"]["notes"]


def test_every_shipped_fair_rule_gets_a_shared_id(export, dataspace, ingest_dir):
    from data2agent.cli import main

    assert main(["assess", str(ingest_dir)]) == 0
    export(variant="curated")
    records = read_proposed(dataspace)
    assessment = json.loads((ingest_dir / "assessment.json").read_text(encoding="utf-8"))
    assert len(records) == len(assessment["results"]) == 12
    by_rule = {
        re.search(r"rule (\S+),", r["provenance"]["notes"]).group(1): (name, r)
        for name, r in records.items()
    }
    for item in assessment["results"]:
        rule_id, principle = item["rule_id"], item["principle"]
        name, record = by_rule[rule_id]
        slug = rule_id.split("-", 1)[1].replace("LICENCE", "LICENSE")
        assert record["criterion"]["id"] == f"FAIR-{principle}_{slug}"
        assert name == record["annotation_id"] == f"{FAMILY}-curated-{principle}-{slug.lower()}"
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name)
    assert f"{FAMILY}-curated-F1-pid-resolvable" in records
    assert not any(name.startswith("ann-") for name in records)


def test_a_family_that_cannot_lead_an_annotation_id_is_refused(export, write_assessment):
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    with pytest.raises(AnnotationExportError, match="starting with a letter or digit"):
        export(family="-synthfam")


def test_ids_that_differ_only_in_case_collide_once_lower_cased(export, tmp_path, manifest):
    payload = _log(manifest)
    payload["verification"]["checks"] = [
        {"id": "HEADERS", "result": "pass"},
        {"id": "headers", "result": "fail"},
    ]
    with pytest.raises(AnnotationExportError, match="collision"):
        export(variant="curated", curation_log=_write_log(tmp_path, payload))


# -- curation log ------------------------------------------------------------------------


def _log(manifest: dict, **overrides) -> dict:
    files = [{"path": f["path"], "sha256": f["sha256"]} for f in manifest["files"]]
    payload = {
        "log_version": "1",
        "date": "2026-01-01",
        "by": "tester",
        "derived_from": {
            "dataset_id": "sha256:" + "a" * 64,
            "files": [{"path": "t.csv", "sha256": "b" * 64}],
        },
        "result": {"dataset_id": manifest["dataset_id"], "files": files},
        "changes": [],
        "verification": {
            "original_unchanged": True,
            "cells_compared": 324,
            "cells_round_trip": 324,
            "checks": [
                {"id": "PACKAGE-VALID", "result": "pass", "detail": "descriptor validates"},
                {"id": "HEADERS", "result": "fail", "detail": "two headers differ"},
            ],
        },
        "open_items": [],
    }
    payload.update(overrides)
    return payload


def _write_log(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "curation-log.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_the_curation_log_becomes_curation_validation_annotations(
    export, dataspace, tmp_path, manifest
):
    log = _write_log(tmp_path, _log(manifest))
    summary = export(variant="curated", curation_log=log)
    records = read_proposed(dataspace)
    by_criterion = {r["criterion"]["id"]: r for r in records.values()}
    assert set(by_criterion) == {
        "CV_ORIGINAL-UNCHANGED",
        "CV_CELL-ROUND-TRIP",
        "CV_PACKAGE-VALID",
        "CV_HEADERS",
    }
    assert summary["by_type"] == {"curation_validation": 4}
    assert sorted(records) == [
        _ann_id(tail, variant="curated")
        for tail in (
            "CV-cell-round-trip",
            "CV-headers",
            "CV-original-unchanged",
            "CV-package-valid",
        )
    ]
    # The log field each record came from stays named in its notes.
    assert "verification.checks[HEADERS]" in by_criterion["CV_HEADERS"]["provenance"]["notes"]
    assert by_criterion["CV_ORIGINAL-UNCHANGED"]["judgement"]["label"] == "conformant"
    assert by_criterion["CV_CELL-ROUND-TRIP"]["judgement"]["label"] == "conformant"
    assert "324 of 324" in by_criterion["CV_CELL-ROUND-TRIP"]["judgement"]["rationale"]
    assert by_criterion["CV_PACKAGE-VALID"]["judgement"]["label"] == "conformant"
    assert by_criterion["CV_PACKAGE-VALID"]["judgement"]["rationale"] == "descriptor validates"
    assert by_criterion["CV_HEADERS"]["judgement"]["label"] == "non_conformant"
    for record in records.values():
        assert record["dataset"]["variant"] == "curated"
        assert record["dataset"]["version"] == manifest["dataset_id"]
        assert record["provenance"]["method"] == "agent_proposal"
        assert record["review"] == {"status": "draft"}
        assert {e["locator"] for e in record["evidence"]} == {
            f"{DATASET_PATH}/README.md",
            f"{DATASET_PATH}/t.csv",
        }


def test_curation_log_failures_map_to_non_conformant(export, dataspace, tmp_path, manifest):
    payload = _log(manifest)
    payload["verification"]["original_unchanged"] = False
    payload["verification"]["cells_round_trip"] = 320
    log = _write_log(tmp_path, payload)
    export(variant="curated", curation_log=log)
    records = {r["criterion"]["id"]: r for r in read_proposed(dataspace).values()}
    assert records["CV_ORIGINAL-UNCHANGED"]["judgement"]["label"] == "non_conformant"
    assert records["CV_CELL-ROUND-TRIP"]["judgement"]["label"] == "non_conformant"
    assert "320 of 324" in records["CV_CELL-ROUND-TRIP"]["judgement"]["rationale"]


def test_a_round_trip_that_compared_nothing_is_insufficient_evidence(
    export, dataspace, tmp_path, manifest
):
    payload = _log(manifest)
    payload["verification"].update(cells_compared=0, cells_round_trip=0)
    export(variant="curated", curation_log=_write_log(tmp_path, payload))
    records = {r["criterion"]["id"]: r for r in read_proposed(dataspace).values()}
    assert records["CV_CELL-ROUND-TRIP"]["judgement"]["label"] == "insufficient_evidence"


def test_a_curation_log_needs_the_curated_variant(export, tmp_path, manifest):
    from data2agent.export import ExportRefused

    with pytest.raises(ExportRefused, match="--variant curated"):
        export(variant="original", curation_log=_write_log(tmp_path, _log(manifest)))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p.update(log_version="2"), "log_version"),
        (
            lambda p: p["result"].update(dataset_id="sha256:" + "9" * 64),
            "ingest the curated dataset",
        ),
        (
            lambda p: p["result"]["files"][0].update(sha256="f" * 64),
            "does not match the ingest manifest",
        ),
        (lambda p: p["result"]["files"].pop(), "omits 1 file"),
        (lambda p: p["result"].update(files=[]), "omits 2 file"),
        (lambda p: p["result"].pop("files"), "must be a list"),
        (
            lambda p: p["result"]["files"].append(dict(p["result"]["files"][0])),
            "listed twice",
        ),
        (lambda p: p["verification"].update(original_unchanged="yes"), "true or false"),
        (lambda p: p["verification"]["checks"][0].update(result="maybe"), "'pass' or 'fail'"),
        (lambda p: p["verification"].update(cells_compared=-1), "non-negative"),
    ],
)
def test_a_malformed_or_mismatched_curation_log_is_refused(
    export, dataspace, tmp_path, manifest, mutate, message
):
    payload = _log(manifest)
    mutate(payload)
    with pytest.raises(AnnotationExportError, match=message):
        export(variant="curated", curation_log=_write_log(tmp_path, payload))
    assert read_proposed(dataspace) == {}


def test_a_curation_check_id_that_is_not_id_safe_is_refused(export, tmp_path, manifest):
    payload = _log(manifest)
    payload["verification"]["checks"][0]["id"] = "bad id"
    with pytest.raises(AnnotationExportError, match="legal annotation_id"):
        export(variant="curated", curation_log=_write_log(tmp_path, payload))


def test_a_curation_log_can_accompany_assessments(
    export, dataspace, tmp_path, manifest, write_assessment
):
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    summary = export(variant="curated", curation_log=_write_log(tmp_path, _log(manifest)))
    assert summary["by_type"] == {"curation_validation": 4, "fair_assessment": 1}


# -- determinism ---------------------------------------------------------------------------


def test_same_inputs_and_timestamp_give_byte_identical_files(
    dataspace, ingest_dir, write_assessment, claim_for, tmp_path
):
    import shutil

    write_assessment(
        "fair",
        [
            result("I1-DATA-FORMATS-OPEN", "I1", "pass", evidence=[claim_for("t.csv")]),
            result("F2-METADATA-PRESENT", "F2", "fail", rationale="none"),
        ],
    )
    twin = tmp_path / "twin"
    shutil.copytree(dataspace, twin)
    kwargs = {
        "dataset_path": DATASET_PATH,
        "family": FAMILY,
        "variant": "original",
        "agent_model": AGENT_MODEL,
        "created_at": CREATED_AT,
    }
    export_annotations(ingest_dir, dataspace=dataspace, **kwargs)
    export_annotations(ingest_dir, dataspace=twin, **kwargs)
    first = sorted((dataspace / "annotations" / "proposed").iterdir())
    second = sorted((twin / "annotations" / "proposed").iterdir())
    assert [p.name for p in first] == [p.name for p in second] and first
    for a, b in zip(first, second, strict=True):
        assert a.read_bytes() == b.read_bytes()


def test_the_summary_is_deterministic_and_machine_readable(export, write_assessment):
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    first = export()
    second = export(overwrite=True)
    assert first == second
    assert json.loads(json.dumps(first)) == first
    assert first["output_dir"] == "annotations/proposed"
    assert first["files"] == [f"{_ann_id('F2-metadata-present')}.json"]


# -- validation ----------------------------------------------------------------------------


def test_records_are_validated_against_the_dataspace_schema_when_present(export, write_assessment):
    pytest.importorskip("jsonschema")
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    summary = export()
    assert summary["schema_validated"] is True
    assert "schema_validation_note" not in summary


def test_without_the_schema_file_the_export_warns_and_says_so(export, dataspace, write_assessment):
    (dataspace / "schemas" / "annotation.schema.json").unlink()
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    summary = export()
    assert summary["schema_validated"] is False
    assert "not found" in summary["schema_validation_note"]
    assert any("NOT validated" in w for w in summary["warnings"])
    assert len(read_proposed(dataspace)) == 1  # still written, still invariant-checked


def test_without_jsonschema_the_export_warns_and_says_so(
    export, dataspace, write_assessment, monkeypatch
):
    monkeypatch.setitem(sys.modules, "jsonschema", None)  # makes `import jsonschema` fail
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    summary = export()
    assert summary["schema_validated"] is False
    assert "jsonschema" in summary["schema_validation_note"]
    assert any("NOT validated" in w for w in summary["warnings"])


def test_the_standin_schema_really_rejects_what_it_claims_to(
    dataspace, ingest_dir, write_assessment
):
    """A stand-in that accepted everything would make the validation tests vacuous."""
    jsonschema = pytest.importorskip("jsonschema")
    write_assessment("fair", [result("F2-METADATA-PRESENT", "F2", "pass")])
    built = build_annotations(
        ingest_dir,
        family=FAMILY,
        variant="original",
        dataset_path=DATASET_PATH,
        agent_model=AGENT_MODEL,
        created_at=CREATED_AT,
    )
    good = built.records[0]
    schema = json.loads((dataspace / "schemas" / "annotation.schema.json").read_text("utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    assert not list(validator.iter_errors(good))

    def broken(edit) -> dict:
        record = json.loads(json.dumps(good))
        edit(record)
        return record

    for record in (
        broken(lambda r: r["review"].update(status="approved")),
        broken(lambda r: r.pop("fair_dimension")),
        broken(lambda r: r["provenance"].pop("agent_model")),
        broken(lambda r: r.update(evidence=[])),
        broken(lambda r: r["evidence"][0].update(locator="/abs/path")),
        broken(lambda r: r["evidence"][0].update(locator="a/../b")),
    ):
        assert list(validator.iter_errors(record))


def test_one_schema_invalid_record_writes_nothing(export, dataspace, write_assessment, monkeypatch):
    """All-or-nothing: the second record breaks the schema, so the first is not written either."""
    pytest.importorskip("jsonschema")
    from data2agent.export import annotations

    write_assessment(
        "fair",
        [
            result("F1-PID-METADATA", "F1", "pass"),
            result("F2-METADATA-PRESENT", "F2", "pass"),
        ],
    )
    real = annotations.build_annotations

    def corrupt(*args, **kwargs):
        built = real(*args, **kwargs)
        built.records[1]["extra"] = 1  # allowed by our invariants, refused by the schema
        return built

    monkeypatch.setattr(annotations, "build_annotations", corrupt)
    with pytest.raises(AnnotationExportError):
        export()
    assert read_proposed(dataspace) == {}
    assert not list((dataspace / "annotations" / "proposed").glob(".*"))


def test_one_invariant_breaking_record_writes_nothing_even_without_a_schema(
    export, dataspace, write_assessment, monkeypatch
):
    from data2agent.export import annotations

    (dataspace / "schemas" / "annotation.schema.json").unlink()
    write_assessment(
        "fair",
        [
            result("F1-PID-METADATA", "F1", "pass"),
            result("F2-METADATA-PRESENT", "F2", "pass"),
        ],
    )
    real = annotations.build_annotations

    def corrupt(*args, **kwargs):
        built = real(*args, **kwargs)
        built.records[1]["evidence"] = []
        return built

    monkeypatch.setattr(annotations, "build_annotations", corrupt)
    with pytest.raises(annotations.ExportInternalError, match="evidence"):
        export()
    assert read_proposed(dataspace) == {}
