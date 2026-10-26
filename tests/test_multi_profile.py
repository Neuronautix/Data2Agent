"""`assess --profile` runs any shipped profile; FAIR output is unchanged by the generalisation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data2agent.cli import main
from data2agent.ingest import ingest
from data2agent.mcp.service import DatasetService
from data2agent.profiles.registry import UnknownProfileError, checks_for

pytest.importorskip("yaml")


@pytest.fixture()
def output(tmp_path: Path) -> Path:
    source = tmp_path / "ds"
    source.mkdir()
    (source / "t.csv").write_text("id,v\n1,2\n2,3\n", encoding="utf-8")
    out = tmp_path / "out"
    ingest(source, out)
    return out


def test_fair_through_the_generic_path_is_the_fair_assessment(output: Path):
    service = DatasetService(output, mode="fair-deterministic")
    assert service.run_profile_check("fair") == service.run_fair_check()


def test_the_registry_resolves_fair_and_refuses_the_rest():
    assert "license_declared" in checks_for("fair")
    with pytest.raises(UnknownProfileError, match="no profile named 'nope'"):
        checks_for("nope")
    with pytest.raises(UnknownProfileError, match="not a valid profile id"):
        checks_for("../fair")


def test_assess_with_an_unknown_profile_is_a_clean_error(output: Path, capsys):
    assert main(["assess", str(output), "--profile", "nope"]) != 0
    assert "nope" in capsys.readouterr().err


def test_assess_fair_still_writes_assessment_json(output: Path):
    assert main(["assess", str(output)]) == 0
    written = json.loads((output / "assessment.json").read_text(encoding="utf-8"))
    assert written["profile"]["id"] == "fair"
