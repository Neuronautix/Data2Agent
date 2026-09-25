"""The deterministic FAIR checks, and the runner's enforcement of rule contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("yaml", reason="profiles need the 'fair' extra")

from data2agent.ingest import ingest  # noqa: E402
from data2agent.mcp import DatasetService  # noqa: E402
from data2agent.profiles.loader import ProfileError, load_profile  # noqa: E402
from data2agent.profiles.model import CheckOutcome  # noqa: E402
from data2agent.profiles.runner import _validate  # noqa: E402


@pytest.fixture
def service(ingested) -> DatasetService:
    return DatasetService(ingested.output_dir, mode="fair-deterministic")


def _by_rule(assessment: dict) -> dict[str, dict]:
    return {result["rule_id"]: result for result in assessment["results"]}


def test_the_example_dataset_assesses_as_expected(service):
    results = _by_rule(service.run_fair_check())
    assert results["F1-PID-METADATA"]["result"] == "pass"
    assert results["F2-METADATA-PRESENT"]["result"] == "pass"
    assert results["F3-METADATA-LINKS-DATA"]["result"] == "pass"
    assert results["F4-METADATA-MACHINE-READABLE"]["result"] == "pass"
    assert results["I1-DATA-FORMATS-OPEN"]["result"] == "pass"
    assert results["R1.1-LICENCE-DECLARED"]["result"] == "pass"
    assert results["R1.2-PROVENANCE-DECLARED"]["result"] == "pass"
    assert results["R1.3-COMMUNITY-STANDARD"]["result"] == "pass"
    # Two genuine defects in the example, both deliberate.
    assert results["I2-VOCABULARY-REFERENCED"]["result"] == "fail"
    assert results["R1.3-MISSING-VALUES-DECLARED"]["result"] == "fail"


def test_unimplemented_rules_stay_unknown_rather_than_disappearing(service):
    """A dropped rule leaves the denominator; an unknown one stays countable."""
    results = _by_rule(service.run_fair_check())
    for rule_id in ("F1-PID-RESOLVABLE", "A1-RETRIEVAL-PROTOCOL"):
        assert results[rule_id]["result"] == "unknown"
        assert results[rule_id]["rationale"]
        assert results[rule_id]["evidence"], "an unknown must still say what was checked"


def test_the_undeclared_na_is_reported_as_a_reusability_defect(service):
    result = _by_rule(service.run_fair_check())["R1.3-MISSING-VALUES-DECLARED"]
    assert result["result"] == "fail"
    assert "NA" in result["rationale"]
    assert "default-sentinels" in result["rationale"]


def test_declaring_the_convention_fixes_that_finding(dataset_copy: Path, tmp_path: Path):
    """The rule is actionable: state the convention and it passes."""
    (dataset_copy / "datapackage.json").write_text(
        json.dumps({"name": "example", "missingValues": ["", "NA"]}), encoding="utf-8"
    )
    result = ingest(dataset_copy, tmp_path / "out")
    service = DatasetService(result.output_dir, mode="fair-deterministic")
    assert _by_rule(service.run_fair_check())["R1.3-MISSING-VALUES-DECLARED"]["result"] == "pass"


def test_every_result_cites_evidence(service):
    for result in service.run_fair_check()["results"]:
        assert result["evidence"], f"{result['rule_id']} returned a verdict with no evidence"


def test_no_result_is_marked_inferred(service):
    for result in service.run_fair_check()["results"]:
        assert result["inferred"] is False


def test_a_deterministic_run_reports_no_unsupported_claims(service):
    assert service.run_fair_check()["unsupported_claims"] == []


def test_a_single_rule_can_be_run_alone(service):
    assessment = service.run_fair_check("F2-METADATA-PRESENT")
    assert len(assessment["results"]) == 1
    assert assessment["results"][0]["rule_id"] == "F2-METADATA-PRESENT"


def test_an_unknown_rule_id_is_rejected(service):
    with pytest.raises(KeyError, match="no rule"):
        service.run_fair_check("F9-INVENTED")


def test_a_dataset_without_metadata_fails_the_findability_rules(tmp_path: Path):
    dataset = tmp_path / "bare"
    dataset.mkdir()
    (dataset / "measurements.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    result = ingest(dataset, tmp_path / "out")
    service = DatasetService(result.output_dir, mode="fair-deterministic")

    results = _by_rule(service.run_fair_check())
    assert results["F2-METADATA-PRESENT"]["result"] == "fail"
    assert results["F1-PID-METADATA"]["result"] == "fail"
    assert results["R1.1-LICENCE-DECLARED"]["result"] == "fail"
    # No metadata at all is a different situation from metadata in the wrong format.
    assert results["F4-METADATA-MACHINE-READABLE"]["result"] == "not_applicable"
    assert results["R1.3-COMMUNITY-STANDARD"]["result"] == "not_applicable"


def test_an_unidentifiable_format_makes_openness_unknown_not_pass(
    dataset_copy: Path, tmp_path: Path
):
    """One unidentified file makes 'all formats are open' unsupportable."""
    (dataset_copy / "recording.xyzzy").write_bytes(b"\x00\x01\x02")
    result = ingest(dataset_copy, tmp_path / "out")
    service = DatasetService(result.output_dir, mode="fair-deterministic")
    assert _by_rule(service.run_fair_check())["I1-DATA-FORMATS-OPEN"]["result"] == "unknown"


@pytest.mark.parametrize(
    ("extension", "expected"), [("ods", "pass"), ("xls", "fail"), ("xlsb", "fail")]
)
def test_every_detected_workbook_format_has_an_openness_verdict(
    tmp_path: Path, extension: str, expected: str
):
    """A newly detected format must not silently turn I1 into 'unknown' (D2A-94)."""
    import shutil

    source = tmp_path / "ds"
    source.mkdir()
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "workbooks"
    shutil.copyfile(fixture / f"animals.{extension}", source / f"animals.{extension}")
    result = ingest(source, tmp_path / "out")
    service = DatasetService(result.output_dir, mode="fair-deterministic")
    assert _by_rule(service.run_fair_check())["I1-DATA-FORMATS-OPEN"]["result"] == expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        # A confirmed BORIS project: open JSON from open-source software.
        (
            b'{"project_format_version": "7.0", "behaviors_conf": {}, "observations": {}}',
            "pass",
        ),
        # JSON under a '.boris' name that is not a project is reported as json.
        (b'{"something": "else"}', "pass"),
        # Not JSON at all: the format stays unknown, and so does the verdict.
        (b"\x00\x01\x02", "unknown"),
    ],
)
def test_a_boris_project_has_an_openness_verdict(tmp_path: Path, content: bytes, expected: str):
    """Detecting BORIS must not leave I1 'unknown' for every dataset holding one."""
    source = tmp_path / "ds"
    source.mkdir()
    (source / "project.boris").write_bytes(content)
    result = ingest(source, tmp_path / "out")
    service = DatasetService(result.output_dir, mode="fair-deterministic")
    assert _by_rule(service.run_fair_check())["I1-DATA-FORMATS-OPEN"]["result"] == expected


def test_an_unreferenced_data_file_is_named_in_the_rationale(dataset_copy: Path, tmp_path: Path):
    (dataset_copy / "orphan.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    result = ingest(dataset_copy, tmp_path / "out")
    service = DatasetService(result.output_dir, mode="fair-deterministic")

    finding = _by_rule(service.run_fair_check())["F3-METADATA-LINKS-DATA"]
    assert finding["result"] == "fail"
    assert "orphan.csv" in finding["rationale"], "a finding must be checkable, not taken on faith"


def test_vocabulary_reference_passes_when_a_namespace_appears(dataset_copy: Path, tmp_path: Path):
    (dataset_copy / "ro-crate-metadata.json").write_text(
        json.dumps({"@context": "https://w3id.org/ro/crate/1.1/context", "@graph": []}),
        encoding="utf-8",
    )
    result = ingest(dataset_copy, tmp_path / "out")
    service = DatasetService(result.output_dir, mode="fair-deterministic")
    assert _by_rule(service.run_fair_check())["I2-VOCABULARY-REFERENCED"]["result"] == "pass"


# -- the runner's contract enforcement ------------------------------------


@pytest.fixture(scope="module")
def rule():
    return load_profile("fair").rule("F2-METADATA-PRESENT")


def test_runner_rejects_a_result_the_rule_does_not_permit(rule):
    narrowed = type(rule)(**{**rule.__dict__, "allowed_results": ("pass", "unknown")})
    with pytest.raises(ProfileError, match="does not permit"):
        _validate(narrowed, CheckOutcome(result="fail", rationale="because"))


def test_runner_rejects_a_verdict_with_no_evidence(rule):
    with pytest.raises(ProfileError, match="no evidence"):
        _validate(rule, CheckOutcome(result="pass", rationale="", evidence=[]))


def test_runner_rejects_a_failure_with_no_rationale(rule):
    with pytest.raises(ProfileError, match="no rationale"):
        _validate(rule, CheckOutcome(result="fail", rationale="", evidence=["clm_x"]))


def test_runner_rejects_a_silent_not_applicable(rule):
    """Even a rule that does not demand a rationale must say why it does not apply."""
    permissive = type(rule)(**{**rule.__dict__, "rationale_required_for": ("fail",)})
    with pytest.raises(ProfileError, match="does not apply"):
        _validate(
            permissive, CheckOutcome(result="not_applicable", rationale="", evidence=["clm_x"])
        )
