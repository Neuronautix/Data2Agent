"""The CLI is the ingestion entry point most users and harnesses will hit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data2agent.cli import main


def test_ingest_writes_the_full_output_contract(example_dataset: Path, tmp_path: Path, capsys):
    output = tmp_path / "agent"
    assert main(["ingest", str(example_dataset), "-o", str(output)]) == 0

    for name in ("manifest.json", "provenance.json", "evidence.json"):
        assert (output / name).is_file()
    assert (output / "mcp" / "server.json").is_file()
    assert (output / "mcp" / "USAGE.md").is_file()
    assert (output / "report" / "dataset-report.md").is_file()

    printed = capsys.readouterr().out
    assert json.loads((output / "manifest.json").read_text())["dataset_id"] in printed

    usage = (output / "mcp" / "USAGE.md").read_text(encoding="utf-8")
    assert "claude mcp add data2agent --" in usage
    assert "codex mcp add data2agent --" in usage
    assert "[mcp_servers.data2agent]" in usage
    assert "Generic MCP JSON configuration" in usage


def test_verify_passes_then_fails_after_drift(dataset_copy: Path, tmp_path: Path, capsys):
    output = tmp_path / "agent"
    assert main(["ingest", str(dataset_copy), "-o", str(output)]) == 0
    capsys.readouterr()

    assert main(["verify", str(output)]) == 0
    (dataset_copy / "animals.csv").write_text("changed\n", encoding="utf-8")
    assert main(["verify", str(output)]) == 1
    assert "animals.csv" in capsys.readouterr().out


def test_report_cites_a_claim_id_for_every_column(example_dataset: Path, tmp_path: Path):
    output = tmp_path / "agent"
    main(["ingest", str(example_dataset), "-o", str(output)])
    report = (output / "report" / "dataset-report.md").read_text(encoding="utf-8")

    table_section = report.split("## Tables")[1].split("## Identifiers")[0]
    column_rows = [line for line in table_section.splitlines() if line.startswith("| `")]
    assert len(column_rows) == 12, "6 columns in animals.csv + 6 in observations.csv"
    for row in column_rows:
        assert "clm_" in row, f"report states a column fact with no claim id: {row}"

    assert "Not determined" in report
    assert "data2agent assess" in report, "the report must point at the separate FAIR layer"

    # Timestamps belong to the run, so they are reported from provenance rather
    # than from the manifest -- but they are reported.
    assert "## This ingest run" in report
    assert "Ingested at: 20" in report
    assert "## Missing-value convention" in report
    assert "default-sentinels" in report


def test_modes_command_marks_unimplemented_modes(capsys):
    assert main(["modes"]) == 0
    printed = capsys.readouterr().out
    assert "NOT IMPLEMENTED" in printed
    assert "available since 0.1.0" in printed


def test_assess_writes_an_assessment_and_lists_findings(
    example_dataset: Path, tmp_path: Path, capsys
):
    pytest.importorskip("yaml", reason="profiles need the 'fair' extra")
    output = tmp_path / "agent"
    main(["ingest", str(example_dataset), "-o", str(output)])
    capsys.readouterr()

    assert main(["assess", str(output)]) == 0
    printed = capsys.readouterr().out

    assessment = json.loads((output / "assessment.json").read_text())
    assert assessment["profile"]["id"] == "fair"
    assert assessment["generator"]["mode"] == "fair-deterministic"
    assert len(assessment["results"]) == 12

    assert "R1.3-MISSING-VALUES-DECLARED" in printed
    assert "unknown" in printed, "unimplemented rules must be visible, not hidden"


def test_assess_can_list_the_rules_without_running_them(
    example_dataset: Path, tmp_path: Path, capsys
):
    pytest.importorskip("yaml", reason="profiles need the 'fair' extra")
    output = tmp_path / "agent"
    main(["ingest", str(example_dataset), "-o", str(output)])
    capsys.readouterr()

    assert main(["assess", str(output), "--list"]) == 0
    printed = capsys.readouterr().out
    assert "F1-PID-METADATA" in printed
    assert "NOT IMPLEMENTED -> always unknown" in printed
    assert not (output / "assessment.json").exists()


def test_relationships_command_writes_candidate_bundle(
    example_dataset: Path, tmp_path: Path, capsys
):
    output = tmp_path / "agent"
    assert main(["ingest", str(example_dataset), "-o", str(output)]) == 0
    capsys.readouterr()

    assert main(["relationships", str(output)]) == 0
    printed = capsys.readouterr().out

    bundle = json.loads((output / "relationships.json").read_text(encoding="utf-8"))
    assert bundle["determined"] is True
    assert bundle["status_counts"] == {"candidate": 1}
    assert bundle["relationships"][0]["status"] == "candidate"
    assert "relationships : 1" in printed


def test_relationships_command_accepts_explicit_declarations(
    example_dataset: Path, tmp_path: Path, capsys
):
    output = tmp_path / "agent"
    assert main(["ingest", str(example_dataset), "-o", str(output)]) == 0
    capsys.readouterr()

    declarations = tmp_path / "relationships.json"
    declarations.write_text(
        json.dumps(
            [
                {
                    "left": "animals.csv",
                    "right": "observations.csv",
                    "left_keys": ["animal_id"],
                    "right_keys": ["animal_id"],
                    "expected_cardinality": "one_to_many",
                    "note": "registry to repeated observations",
                }
            ]
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "relationships",
                str(output),
                "--declarations",
                str(declarations),
            ]
        )
        == 0
    )

    bundle = json.loads((output / "relationships.json").read_text(encoding="utf-8"))
    assert bundle["status_counts"] == {"declared": 1}
    relation = bundle["relationships"][0]
    assert relation["status"] == "declared"
    assert relation["basis"]["declaration_source"]["sha256"]
    assert relation["cardinality"] == "one_to_many"


def test_strict_missing_resolves_no_tokens(example_dataset: Path, tmp_path: Path, capsys):
    output = tmp_path / "agent"
    assert main(["ingest", str(example_dataset), "-o", str(output), "--strict-missing"]) == 0
    assert "strict-empty-only" in capsys.readouterr().out

    manifest = json.loads((output / "manifest.json").read_text())
    strain = next(
        column
        for column in manifest["tables"]["animals.csv"]["columns"]
        if column["name"] == "strain"
    )
    assert strain["missing"] == 0


def test_missing_tokens_override_is_recorded(example_dataset: Path, tmp_path: Path):
    output = tmp_path / "agent"
    assert (
        main(["ingest", str(example_dataset), "-o", str(output), "--missing-tokens", "NA,WT"]) == 0
    )

    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["missing_value_convention"]["id"] == "custom"
    genotype = next(
        column
        for column in manifest["tables"]["animals.csv"]["columns"]
        if column["name"] == "genotype"
    )
    assert genotype["missing_sentinel"] == 24
