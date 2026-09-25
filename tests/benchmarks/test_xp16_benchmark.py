"""XP16 benchmark tooling: scorer, gold evaluator and builder, on synthetic data only.

No real dataset is touched here. Every workbook and export is fabricated in
``tmp_path`` with made-up animals, so the tests pin the *behaviour* of the
public scripts (comparison rules, abstention scoring, citation checks, unit-of-
analysis aggregation, byte-identical rebuilds) without carrying any data.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")
yaml = pytest.importorskip("yaml")

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "benchmarks" / "xp16"
sys.path.insert(0, str(SCRIPTS))

import xp16lib  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


score = _load("xp16_score", "03_score.py")
freeze = _load("xp16_freeze", "01_freeze.py")
builder = _load("xp16_build", "02_build_gold.py")


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """A tiny synthetic package: a registry with a banner row, a binned export."""
    origin = tmp_path / "origin"
    (origin / "sub").mkdir(parents=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Reg"
    ws["C1"] = "Body weight"  # banner row above the real header
    for col, name in enumerate(["Cage", "Tail", "Key", "Group", "Weight"], start=1):
        ws.cell(2, col, name)
    rows = [
        ("C-1", "I", "C-1_I", "wt", 20.0),
        ("C-1", "II", "C-1_II", "wt", 22.0),
        ("C-2", "I", "C-2_I", "ko", 25.0),
        ("C-2", "II", "C-2_II", "ko", "?"),
    ]
    for r, values in enumerate(rows, start=3):
        for c, v in enumerate(values, start=1):
            ws.cell(r, c, v)
    wb.save(origin / "registry.xlsx")

    lines = [
        "\tSubjects:\t\tx\tx",
        "\tBehaviors:\t\tevent\tevent",
        "Obs\tLength\tInterval\tTotal duration\tNumber",
    ]
    for animal in ("C1-I", "C1-II", "C2-I"):
        for b in range(3):
            lines.append(
                f"{animal}\t900\t{b * 300}-{(b + 1) * 300}\t{b + (2 if animal == 'C2-I' else 1)}\t1"
            )
    # bytes, not text: write_text would turn "\r\n" into "\r\r\n" on Windows
    (origin / "sub" / "bins.tsv").write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))

    pkg = tmp_path / "pkg"
    (pkg / "config").mkdir(parents=True)
    (pkg / "adjudication").mkdir()
    rc = _run(freeze, ["--source", str(origin), "--package", str(pkg)])
    assert rc == 0

    (pkg / "config" / "tables.yaml").write_text(
        yaml.safe_dump(
            {
                "transforms": {
                    "reg_to_meas": {"regex": r"C-(?P<c>\d)_(?P<t>[IVX]+)", "template": "C{c}-{t}"}
                },
                "tables": {
                    "reg": {"file": "registry.xlsx", "sheet": "Reg", "header_rows": [2]},
                    "bins": {
                        "kind": "delimited",
                        "file": "sub/bins.tsv",
                        "delimiter": "\t",
                        "header_rows": [2, 3],
                        "drop": ["Behaviors:", "Subjects:"],
                        "first_row": 4,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (pkg / "adjudication" / "decisions.yaml").write_text(
        yaml.safe_dump({"questions": [{"id": "OQ1", "status": "open", "answer": None}]}),
        encoding="utf-8",
    )
    spec = {
        "questions": [
            {
                "id": "Q-count",
                "category": "inventory",
                "question": "How many animals?",
                "answer_type": "integer",
                "requires": ["header_detection"],
                "compute": {"op": "count", "table": "reg", "distinct": "Key"},
            },
            {
                "id": "Q-unit",
                "category": "aggregation",
                "question": "Per-animal total, mean by nothing",
                "answer_type": "table",
                "requires": ["boris_tsv_preamble", "unit_aggregation"],
                "compute": {
                    "op": "group_stats",
                    "table": "bins",
                    "unit": "Obs",
                    "value": "event | Total duration",
                    "reduce": "sum",
                },
                "score": {"type": "table", "key": [], "fields": {"n": {"type": "integer"}}},
            },
            {
                "id": "Q-missing-weight",
                "category": "abstention",
                "question": "Weight of C-2_II?",
                "answer_type": "abstain",
                "requires": ["abstention"],
                "compute": {
                    "op": "abstain",
                    "evidence": [
                        {
                            "op": {
                                "op": "cell",
                                "table": "reg",
                                "where": [{"col": "Key", "value": "C-2_II"}],
                                "column": "Weight",
                            },
                            "expect": "?",
                        }
                    ],
                },
            },
            {
                "id": "Q-blocked",
                "category": "join",
                "question": "Group of each measured animal?",
                "answer_type": "table",
                "requires": ["crosswalk"],
                "blocked_by": ["OQ1"],
                "compute": {
                    "op": "group_stats",
                    "table": "bins",
                    "unit": "Obs",
                    "value": "event | Total duration",
                    "reduce": "sum",
                    "by": ["Group"],
                    "join": {
                        "table": "reg",
                        "left_key": "Obs",
                        "right_key": "Key",
                        "right_transform": "reg_to_meas",
                        "columns": ["Group"],
                    },
                },
            },
        ]
    }
    (pkg / "config" / "questions.spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return pkg


def _run(module, argv: list[str]) -> int:
    old = sys.argv
    sys.argv = ["x", *argv]
    try:
        return module.main()
    finally:
        sys.argv = old


# ---------------------------------------------------------------- comparison rules


def test_numeric_tolerance_is_absolute_and_inclusive():
    assert score.compare(1.0, 1.0009, {"type": "number", "abs": 0.001})[0]
    assert not score.compare(1.0, 1.002, {"type": "number", "abs": 0.001})[0]
    assert score.compare(100.0, "100.4", {"type": "number", "rel": 0.005})[0]


def test_set_comparison_ignores_order_case_and_whitespace_but_not_membership():
    assert score.compare(["A-1", "B-2"], ["b-2 ", "a-1"], {"type": "set"})[0]
    ok, why = score.compare(["A-1", "B-2"], ["A-1"], {"type": "set"})
    assert not ok and "missing" in why


def test_table_comparison_matches_rows_by_key_and_rejects_extras():
    rule = {
        "type": "table",
        "key": ["g"],
        "fields": {"n": {"type": "integer"}, "mean": {"type": "number", "abs": 0.01}},
    }
    expected = [{"g": "wt", "n": 2, "mean": 21.0}, {"g": "ko", "n": 1, "mean": 25.0}]
    assert score.compare(
        expected, [{"g": "ko", "n": 1, "mean": 25.004}, {"g": "wt", "n": 2, "mean": 21}], rule
    )[0]
    assert not score.compare(
        expected, [{"g": "wt", "n": 3, "mean": 21.0}, {"g": "ko", "n": 1, "mean": 25}], rule
    )[0]
    extra = [*expected, {"g": "het", "n": 1, "mean": 0}]
    assert not score.compare(expected, extra, rule)[0]


def test_object_comparison_scores_only_declared_fields():
    rule = {"type": "object", "fields": {"all_equal": {"type": "boolean"}, "bad": {"type": "set"}}}
    assert score.compare(
        {"all_equal": False, "bad": ["x"]}, {"all_equal": "no", "bad": ["X"], "extra": 1}, rule
    )[0]


# ---------------------------------------------------------------- abstention


@pytest.mark.parametrize(
    ("expected", "entry", "outcome", "points"),
    [
        ("ABSTAIN", {"abstain": True}, "correct_abstention", 1),
        ("PENDING-OQ1", {"answer": "cannot be determined"}, "correct_abstention", 1),
        ("ABSTAIN", {"answer": 42}, "confident_on_unanswerable", -1),
        ("PENDING-OQ1", {"answer": [{"g": "wt"}]}, "confident_on_unanswerable", -1),
        (7, {"abstain": True}, "over_abstention", 0),
        (7, {"answer": "unknown"}, "over_abstention", 0),
        (7, None, "missing", 0),
        (7, {"answer": 7}, "correct", 1),
        (7, {"answer": "unknown genotype"}, "incorrect", 0),
    ],
)
def test_abstention_scoring(expected, entry, outcome, points):
    q = {"id": "Q", "category": "c", "answer_type": "integer", "expected": expected, "requires": []}
    r = score.score_question(q, entry, None)
    assert (r["outcome"], r["points"]) == (outcome, points)


# ---------------------------------------------------------------- citations


def test_citations_are_checked_against_checksums_and_bytes(package: Path):
    checksums = json.loads((package / "checksums.json").read_text(encoding="utf-8"))
    sha = {f["path"]: f["sha256"] for f in checksums["files"]}
    ctx = score.CitationContext(checksums, package / "source")
    q = {
        "id": "Q",
        "category": "retrieval",
        "answer_type": "number",
        "expected": 22.0,
        "requires": [],
    }

    def cite(**src):
        return score.citation_status({"answer": 22.0, "sources": [src]}, q, ctx)["status"]

    assert (
        cite(file="registry.xlsx", sha256=sha["registry.xlsx"], sheet="Reg", cell="E4")
        == "verified"
    )
    # right file, wrong cell: the cited cell does not hold the answer
    assert (
        cite(file="registry.xlsx", sha256=sha["registry.xlsx"], sheet="Reg", cell="E3") == "invalid"
    )
    assert cite(file="registry.xlsx", sha256="0" * 64, sheet="Reg", cell="E4") == "invalid"
    assert cite(file="nope.xlsx", sheet="Reg", cell="E4") == "invalid"
    assert (
        cite(file="registry.xlsx", sha256=sha["registry.xlsx"], sheet="Nope", cell="E4")
        == "invalid"
    )
    assert cite(file="registry.xlsx", sheet="Reg", cell="E4") == "sha_unverified"
    assert score.citation_status({"answer": 22.0}, q, ctx)["status"] == "missing"

    q_line = {**q, "expected": 3}
    tsv = "sub/bins.tsv"
    ok = score.citation_status(
        {"answer": 3, "sources": [{"file": tsv, "sha256": sha[tsv], "line": 6, "field": 4}]},
        q_line,
        ctx,
    )
    assert ok["status"] == "verified"
    gold_style = score.citation_status(
        {"answer": 3, "sources": [{"file": tsv, "sha256": sha[tsv], "range": "line 6, field 4"}]},
        q_line,
        ctx,
    )
    assert gold_style["status"] == "verified"


# ---------------------------------------------------------------- evaluator


def test_unit_of_analysis_reduction_counts_animals_not_rows(package: Path):
    config = yaml.safe_load((package / "config" / "tables.yaml").read_text(encoding="utf-8"))
    src = xp16lib.FileRowSource(package / "source", config)
    per_animal = xp16lib.evaluate(
        src,
        config,
        {
            "op": "group_stats",
            "table": "bins",
            "unit": "Obs",
            "value": "event | Total duration",
            "reduce": "sum",
        },
    ).answer
    per_row = xp16lib.evaluate(
        src, config, {"op": "group_stats", "table": "bins", "value": "event | Total duration"}
    ).answer
    # 3 animals x 3 bins: per-animal sums 6, 6, 9
    assert per_animal[0]["n"] == 3 and per_animal[0]["mean"] == 7
    assert per_row[0]["n"] == 9
    assert per_animal[0]["sem"] != per_row[0]["sem"]


def test_header_below_a_banner_and_non_numeric_markers_are_reported(package: Path):
    config = yaml.safe_load((package / "config" / "tables.yaml").read_text(encoding="utf-8"))
    src = xp16lib.FileRowSource(package / "source", config)
    table = src.table("reg")
    assert table.columns == ["Cage", "Tail", "Key", "Group", "Weight"]
    result = xp16lib.evaluate(
        src, config, {"op": "group_stats", "table": "reg", "value": "Weight", "by": ["Group"]}
    )
    assert {r["Group"]: r["n"] for r in result.answer} == {"ko": 1, "wt": 2}
    assert "non-numeric" in result.computation and "'?'" in result.computation


def test_declared_transform_maps_unmatched_forms_to_none():
    tf = xp16lib.make_transform(
        [
            {"regex": r"B9(?P<n>\d{2})", "template": "{n}"},
            {"regex": r"(?P<n>C\d)", "template": "{n}"},
        ]
    )
    assert (tf("B912"), tf("C7"), tf("X1")) == ("12", "C7", None)


# ---------------------------------------------------------------- builder


def test_build_is_byte_identical_pending_is_explicit_and_source_untouched(package: Path):
    before = {p: p.read_bytes() for p in (package / "source").rglob("*") if p.is_file()}
    assert _run(builder, ["--package", str(package)]) == 0
    assert _run(builder, ["--package", str(package), "--check"]) == 0
    after = {p: p.read_bytes() for p in (package / "source").rglob("*") if p.is_file()}
    assert before == after

    gold = yaml.safe_load((package / "gold" / "questions.yaml").read_text(encoding="utf-8"))
    by_id = {q["id"]: q for q in gold["questions"]}
    assert by_id["Q-count"]["expected"] == 4
    assert by_id["Q-count"]["sources"][0]["sheet"] == "Reg"
    assert by_id["Q-missing-weight"]["expected"] == "ABSTAIN"
    blocked = by_id["Q-blocked"]
    assert blocked["expected"] == "PENDING-OQ1" and blocked["status"] == "pending"
    assert {r["Group"] for r in blocked["provisional_answer"]} == {"ko", "wt"}

    # answering the owner question turns the provisional answer into gold
    decisions = package / "adjudication" / "decisions.yaml"
    decisions.write_text(
        yaml.safe_dump({"questions": [{"id": "OQ1", "status": "answered", "answer": "yes"}]}),
        encoding="utf-8",
    )
    assert _run(builder, ["--package", str(package), "--check"]) == 1
    assert _run(builder, ["--package", str(package)]) == 0
    gold = yaml.safe_load((package / "gold" / "questions.yaml").read_text(encoding="utf-8"))
    blocked = {q["id"]: q for q in gold["questions"]}["Q-blocked"]
    assert blocked["status"] == "answerable" and isinstance(blocked["expected"], list)


def test_score_run_breaks_down_by_capability_and_filters(package: Path):
    assert _run(builder, ["--package", str(package)]) == 0
    gold = yaml.safe_load((package / "gold" / "questions.yaml").read_text(encoding="utf-8"))
    answers = {
        "Q-count": {"answer": 4},
        "Q-unit": {"answer": [{"n": 9}]},  # counted rows, not animals
        "Q-missing-weight": {"answer": 0},  # confident on unanswerable
        "Q-blocked": {"abstain": True},
    }
    report = score.score_run(gold["questions"], answers)
    assert report["total"]["points"] == 1 + 0 - 1 + 1
    assert report["confident_on_unanswerable"] == ["Q-missing-weight"]
    assert report["by_capability"]["unit_aggregation"]["incorrect"] == 1
    subset = score.score_run(gold["questions"], answers, capabilities={"header_detection"})
    assert subset["scored"] == 1 and subset["excluded_by_capability_filter"] == 3


def test_freeze_refuses_to_refreeze_a_drifted_snapshot(package: Path, tmp_path: Path):
    origin = tmp_path / "origin"
    (origin / "sub" / "bins.tsv").write_text("changed\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="refusing to refreeze"):
        _run(freeze, ["--source", str(origin), "--package", str(package)])
