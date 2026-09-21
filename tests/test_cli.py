"""The CLI is the ingestion entry point most users and harnesses will hit."""

from __future__ import annotations

import json
from pathlib import Path

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
    assert "FAIR assessment is a separate profile" in report


def test_modes_command_marks_unimplemented_modes(capsys):
    assert main(["modes"]) == 0
    printed = capsys.readouterr().out
    assert "NOT IMPLEMENTED" in printed
    assert "available since 0.1.0" in printed
