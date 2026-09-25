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
baseline = _load("xp16_baseline", "05_baseline.py")
ingest_step = _load("xp16_ingest", "06_ingest.py")


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
        ("C-3", "I", "   ", "wt", None),  # blank identifier: never a unit
        ("C-3", "II", "C-3_II", "  ", None),  # blank group key: never a group
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
    # a footer block after the declared last_row, which must never be read as data
    lines += ["", "Totals\t\t\t999\t9", "C9-IX\t900\t0-300\t50\t1"]
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
                    "reg": {
                        "file": "registry.xlsx",
                        "sheet": "Reg",
                        "header_rows": [2],
                        "service_table": "registry.xlsx#Reg",
                    },
                    "bins": {
                        "kind": "delimited",
                        "file": "sub/bins.tsv",
                        "delimiter": "\t",
                        "header_rows": [2, 3],
                        "drop": ["Behaviors:", "Subjects:"],
                        "first_row": 4,
                        "last_row": 12,
                        "service_table": "sub/bins.tsv",
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
                "on_answer": {"OQ1": {"yes": "compute", "no": "abstain", "unknown": "abstain"}},
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
    assert by_id["Q-count"]["expected"] == 5  # C-3_II counts; the blank-key row does not
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
        "Q-count": {"answer": 5},
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


# ---------------------------------------------------------------- owner answers


def _answer(package: Path, answer: str) -> None:
    (package / "adjudication" / "decisions.yaml").write_text(
        yaml.safe_dump({"questions": [{"id": "OQ1", "status": "answered", "answer": answer}]}),
        encoding="utf-8",
    )


def _blocked(package: Path) -> dict:
    gold = yaml.safe_load((package / "gold" / "questions.yaml").read_text(encoding="utf-8"))
    return {q["id"]: q for q in gold["questions"]}["Q-blocked"]


@pytest.mark.parametrize(
    ("answer", "status", "expected_kind"),
    [
        ("yes", "answerable", list),  # the hypothesis holds: provisional becomes gold
        ("No", "abstain", str),  # it does not: the provisional computation is void
        ("unknown", "abstain", str),  # owner-confirmed unknown: "cannot be determined"
    ],
)
def test_only_an_affirmative_answer_promotes_the_provisional_computation(
    package: Path, answer: str, status: str, expected_kind: type
):
    _answer(package, answer)
    assert _run(builder, ["--package", str(package)]) == 0
    blocked = _blocked(package)
    assert blocked["status"] == status
    assert isinstance(blocked["expected"], expected_kind)
    assert "provisional_answer" not in blocked
    assert blocked["resolved_by"] == {"OQ1": answer.casefold()}
    if status == "abstain":
        assert blocked["expected"] == "ABSTAIN"


def test_an_answer_without_a_declared_outcome_fails_the_build(package: Path):
    _answer(package, "only for males")
    with pytest.raises(SystemExit, match="declares no outcome"):
        _run(builder, ["--package", str(package)])


def test_an_answered_decision_on_a_question_without_on_answer_fails(package: Path):
    spec_path = package / "config" / "questions.spec.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    for q in spec["questions"]:
        q.pop("on_answer", None)
    spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    _answer(package, "yes")
    with pytest.raises(SystemExit, match="declares no outcome"):
        _run(builder, ["--package", str(package)])


def test_a_no_answer_can_select_a_declared_alternative_computation(package: Path):
    spec_path = package / "config" / "questions.spec.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    alternative = {"op": "count", "table": "bins", "distinct": "Obs"}
    for q in spec["questions"]:
        if q["id"] == "Q-blocked":
            q["on_answer"]["OQ1"]["no"] = alternative
    spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    _answer(package, "no")
    assert _run(builder, ["--package", str(package)]) == 0
    blocked = _blocked(package)
    assert (blocked["status"], blocked["expected"]) == ("answerable", 3)


# ---------------------------------------------------------------- reader edges


def test_delimited_last_row_excludes_a_footer_block(package: Path):
    config = yaml.safe_load((package / "config" / "tables.yaml").read_text(encoding="utf-8"))
    table = xp16lib.FileRowSource(package / "source", config).table("bins")
    assert len(table.rows) == 9 and table.rows[-1].locator == 12
    assert {r.values["Obs"] for r in table.rows} == {"C1-I", "C1-II", "C2-I"}


def test_blank_identifiers_and_group_keys_are_never_counted(package: Path):
    config = yaml.safe_load((package / "config" / "tables.yaml").read_text(encoding="utf-8"))
    src = xp16lib.FileRowSource(package / "source", config)
    assert (
        xp16lib.evaluate(src, config, {"op": "count", "table": "reg", "distinct": "Key"}).answer
        == 5
    )
    assert (
        "   "
        not in xp16lib.evaluate(
            src, config, {"op": "values", "table": "reg", "column": "Key"}
        ).answer
    )
    groups = xp16lib.evaluate(
        src, config, {"op": "group_count", "table": "reg", "by": ["Group"], "distinct": "Key"}
    )
    assert groups.answer == [{"Group": "ko", "n": 2}, {"Group": "wt", "n": 2}]
    assert "2 row(s) with a blank group key or id excluded" in groups.computation
    rows = xp16lib.evaluate(src, config, {"op": "group_count", "table": "reg", "by": ["Group"]})
    assert rows.answer == [{"Group": "ko", "n": 2}, {"Group": "wt", "n": 3}]


# ---------------------------------------------------------------- citation strength


def test_a_citation_set_is_only_as_strong_as_its_weakest_source(package: Path):
    checksums = json.loads((package / "checksums.json").read_text(encoding="utf-8"))
    sha = {f["path"]: f["sha256"] for f in checksums["files"]}
    ctx = score.CitationContext(checksums, package / "source")
    q = {"id": "Q", "category": "retrieval", "answer_type": "table", "expected": [], "requires": []}
    verified = {
        "file": "registry.xlsx",
        "sha256": sha["registry.xlsx"],
        "sheet": "Reg",
        "cell": "E4",
    }
    sha_only = {"file": "sub/bins.tsv", "sha256": sha["sub/bins.tsv"]}  # no locator to check
    result = score.citation_status({"answer": [], "sources": [verified, sha_only]}, q, ctx)
    assert result["status"] == "sha_only"
    assert result["per_source"] == ["verified", "sha_only"]
    unpinned = {"file": "registry.xlsx", "sheet": "Reg", "cell": "E4"}
    result = score.citation_status({"answer": [], "sources": [verified, unpinned]}, q, ctx)
    assert result["status"] == "sha_unverified"
    both = score.citation_status({"answer": [], "sources": [verified, verified]}, q, ctx)
    assert both["status"] == "verified"
    # a composed key column (fields 1 and 3) over a line range, as the gold writes it
    composed = {
        "file": "sub/bins.tsv",
        "sha256": sha["sub/bins.tsv"],
        "range": "lines 4-12, field 1+3",
    }
    assert (
        score.citation_status({"answer": [], "sources": [composed]}, q, ctx)["status"] == "verified"
    )
    too_wide = {**composed, "range": "lines 4-12, field 1+30"}
    assert (
        score.citation_status({"answer": [], "sources": [too_wide]}, q, ctx)["status"] == "invalid"
    )


# ---------------------------------------------------------------- declared ingest + baseline


@pytest.mark.parametrize(
    ("gold", "service", "ok"),
    [
        ("W1_Date", "Weights / W1_Date", True),  # extra upper label on the service side
        ("event | Total duration", "event / Total duration", True),
        ("event | Total duration", "Total duration.2", False),  # upper label missing
        ("Genotype #2", "Genotype.1", True),  # both dedupe conventions
        ("0.4", "0.4", True),  # a numeric header is not a dedupe suffix
        ("0.4", "0.02", False),
        ("Weight", "Group", False),
    ],
)
def test_service_labels_must_end_with_the_gold_header_parts(gold, service, ok):
    names = {service, "Genotype", "Total duration"}
    assert baseline._labels_compatible(gold, service, names) is ok


def _conditions(package: Path) -> None:
    config = package / "config"
    (config / "layouts.json").write_text(
        json.dumps(
            {
                "layouts": {
                    "registry.xlsx#Reg": {"header_row": 2},
                    "sub/bins.tsv": {"header_row": 2, "header_rows": 2, "upper_label_fill": "none"},
                }
            }
        ),
        encoding="utf-8",
    )
    (config / "conditions.json").write_text(
        json.dumps(
            {
                "conditions": {
                    "undeclared": {"output": "ingest_undeclared"},
                    "declared": {"output": "ingest_declared", "layout": "layouts.json"},
                }
            }
        ),
        encoding="utf-8",
    )


def test_declared_ingest_is_recorded_and_changes_what_the_baseline_retrieves(package: Path):
    _conditions(package)
    assert _run(builder, ["--package", str(package)]) == 0
    for condition in ("undeclared", "declared"):
        ingest_step.ingest(package, condition)
    record = json.loads((package / "ingest_declared" / "condition.json").read_text("utf-8"))
    layout_sha = xp16lib.sha256_file(package / "config" / "layouts.json")
    assert record["condition"] == "declared"
    assert record["declarations"]["layout"]["sha256"] == layout_sha
    manifest = json.loads((package / "ingest_declared" / "manifest.json").read_text("utf-8"))
    assert manifest["layout_declaration"]["sha256"] == layout_sha
    undeclared = json.loads((package / "ingest_undeclared" / "condition.json").read_text("utf-8"))
    assert undeclared["declarations"] == {}
    with pytest.raises(SystemExit, match="exists"):
        ingest_step.ingest(package, "declared")  # never silently overwritten

    outcomes = {}
    for condition in ("undeclared", "declared"):
        out = package / f"baseline_{condition}.json"
        argv = ["--package", str(package), "--ingest", str(package / f"ingest_{condition}")]
        assert _run(baseline, [*argv, "--out", str(out)]) == 0
        report = json.loads(out.read_text("utf-8"))
        assert report["condition"]["condition"] == condition
        outcomes[condition] = {
            q["id"]: (q.get("retrieval") or q.get("evidence"))["outcome"]
            for q in report["questions"]
        }
    # the per-animal export question needs the behaviour label, which only the
    # declared two-row header carries
    assert outcomes["undeclared"]["Q-unit"] == "fail"
    assert outcomes["declared"]["Q-unit"] == "pass"
    assert outcomes["declared"]["Q-count"] == "pass"
    # the export carries a footer after last_row that the service's aggregate cannot
    # exclude by position, so the native probe refuses instead of aggregating it
    report = json.loads((package / "baseline_declared.json").read_text("utf-8"))
    native = {q["id"]: q.get("native_aggregate") for q in report["questions"]}
    assert native["Q-unit"]["status"] == "mixes_blocks"


def test_the_native_unit_probe_matches_the_gold_on_a_clean_table(package: Path):
    _conditions(package)
    # drop the footer: the same export without rows after the data
    tsv = package / "source" / "sub" / "bins.tsv"
    lines = tsv.read_bytes().split(b"\r\n")[:12]
    tsv.write_bytes(b"\r\n".join(lines) + b"\r\n")
    checksums = json.loads((package / "checksums.json").read_text("utf-8"))
    for f in checksums["files"]:
        if f["path"] == "sub/bins.tsv":
            f["sha256"] = xp16lib.sha256_file(tsv)
    checksums["dataset_id"] = freeze.fold_dataset_id(
        [(f["path"], f["sha256"]) for f in checksums["files"]]
    )
    (package / "checksums.json").write_text(json.dumps(checksums), encoding="utf-8")
    assert _run(builder, ["--package", str(package)]) == 0
    ingest_step.ingest(package, "declared")
    out = package / "baseline.json"
    argv = ["--package", str(package), "--ingest", str(package / "ingest_declared")]
    assert _run(baseline, [*argv, "--out", str(out)]) == 0
    report = json.loads(out.read_text("utf-8"))
    native = {q["id"]: q.get("native_aggregate") for q in report["questions"]}
    assert native["Q-unit"]["status"] == "pass", native["Q-unit"]


def test_a_banner_cell_above_the_header_is_read_through_inspect_table(package: Path):
    _conditions(package)
    ingest_step.ingest(package, "declared")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from data2agent.mcp.service import DatasetService

    config = yaml.safe_load((package / "config" / "tables.yaml").read_text(encoding="utf-8"))
    svc = DatasetService(package / "ingest_declared", source_dir=package / "source")
    src = baseline.ServiceRowSource(svc, config, xp16lib.FileRowSource(package / "source", config))
    assert src.raw_cell("registry.xlsx", "Reg", "C1") == "Body weight"  # banner row
    assert src.raw_cell("registry.xlsx", "Reg", "C2") == "Key"  # the header row itself
    assert src.raw_cell("registry.xlsx", "Reg", "C3") == "C-1_I"  # a data row
    assert "inspect_table(include_rows_above_data=True)" in src.tools_used


def test_a_condition_may_readdress_a_table_but_only_a_declared_one(package: Path):
    _conditions(package)
    conditions_path = package / "config" / "conditions.json"
    conditions = json.loads(conditions_path.read_text("utf-8"))
    conditions["conditions"]["declared"]["service_tables"] = {"reg": "registry.xlsx#Reg"}
    conditions["conditions"]["typo"] = {
        "output": "ingest_typo",
        "service_tables": {"no_such_table": "registry.xlsx#Reg"},
    }
    conditions_path.write_text(json.dumps(conditions), encoding="utf-8")
    assert _run(builder, ["--package", str(package)]) == 0
    ingest_step.ingest(package, "declared")
    record = json.loads((package / "ingest_declared" / "condition.json").read_text("utf-8"))
    assert record["service_tables"] == {"reg": "registry.xlsx#Reg"}
    assert record["data2agent_src"] == "repository"
    out = package / "b.json"
    argv = ["--package", str(package), "--ingest", str(package / "ingest_declared")]
    assert _run(baseline, [*argv, "--out", str(out)]) == 0
    ingest_step.ingest(package, "typo")
    argv = ["--package", str(package), "--ingest", str(package / "ingest_typo")]
    with pytest.raises(SystemExit, match="unknown gold table"):
        _run(baseline, [*argv, "--out", str(out)])


# ---------------------------------------------------------------- windows and joins


class _Tables:
    """A minimal RowSource over in-memory tables, for evaluator unit tests."""

    def __init__(self, **tables):
        self.tables = tables

    def table(self, table_id):
        return self.tables[table_id]

    def file_sha(self, file):
        return "0" * 64

    def raw_cell(self, file, sheet, cell):
        raise KeyError(cell)


def _mem_table(table_id, columns, rows):
    return xp16lib.Table(
        table_id,
        f"{table_id}.csv",
        "0" * 64,
        None,
        list(columns),
        {c: str(i + 1) for i, c in enumerate(columns)},
        [
            xp16lib.Row(dict(zip(columns, r, strict=True)), n, {})
            for n, r in enumerate(rows, start=2)
        ],
        [1],
    )


def test_span_filters_select_full_and_partial_bins():
    bins = _mem_table(
        "bins",
        ["obs", "interval", "v"],
        [["o1", "0.000-300.000", 1], ["o1", "300.000-600.000", 2], ["o1", "600.000-774.5", 4]],
    )
    src = _Tables(bins=bins)
    full = [{"col": "interval", "op": "span_ge", "value": 300}]
    part = [{"col": "interval", "op": "span_lt", "value": 300}]
    assert (
        xp16lib.evaluate(
            src, {}, {"op": "sum", "table": "bins", "column": "v", "where": full}
        ).answer
        == 3
    )
    assert (
        xp16lib.evaluate(
            src, {}, {"op": "sum", "table": "bins", "column": "v", "where": part}
        ).answer
        == 4
    )
    assert not xp16lib._match(bins.rows[0], {"col": "v", "op": "span_ge", "value": 0})


def test_a_repeated_right_key_joins_only_when_declared_and_consistent():
    left = _mem_table("left", ["obs", "v"], [["o1", 1], ["o1", 2], ["o2", 5], ["o3", 7]])
    right = _mem_table(
        "right",
        ["obs", "animal", "group"],
        [["o1", "A", "g1"], ["o1", "A", "g1"], ["o2", "B", "g2"], ["o3", "C", "g1"]],
    )
    src = _Tables(left=left, right=right)
    spec = {
        "op": "group_stats",
        "table": "left",
        "unit": "obs",
        "value": "v",
        "reduce": "sum",
        "join": {
            "table": "right",
            "left_key": "obs",
            "right_key": "obs",
            "columns": ["animal"],
            "where": [{"col": "group", "value": "g1"}],
            "right_rows_agree": True,
            "allow_unmatched": True,
        },
    }
    result = xp16lib.evaluate(src, {}, spec)
    assert result.answer[0]["n"] == 2 and result.answer[0]["mean"] == 5  # o1: 3, o3: 7
    assert "1 row(s) matched no right-table row" in result.computation  # o2, filtered out

    undeclared = {**spec, "join": {**spec["join"], "right_rows_agree": False}}
    with pytest.raises(xp16lib.GoldError, match="not unique"):
        xp16lib.evaluate(src, {}, undeclared)
    right.rows[1].values["animal"] = "Z"  # the repeats now disagree
    with pytest.raises(xp16lib.GoldError, match="disagree"):
        xp16lib.evaluate(src, {}, spec)


def test_the_baseline_reruns_the_computation_the_gold_actually_used():
    provisional = {"op": "count", "table": "t"}
    alternative = {"op": "sum", "table": "t", "column": "v"}
    q = {
        "id": "Q",
        "blocked_by": ["OQ1"],
        "compute": provisional,
        "on_answer": {"OQ1": {"yes": "compute", "no": alternative, "unknown": "abstain"}},
    }
    answered = {"OQ1": {"id": "OQ1", "status": "answered", "answer": "no"}}
    assert baseline.effective_compute(q, answered, set()) == alternative
    answered_yes = {"OQ1": {"id": "OQ1", "status": "answered", "answer": "yes"}}
    assert baseline.effective_compute(q, answered_yes, set()) == provisional
    # still open: the gold is PENDING, the provisional computation is what exists
    assert baseline.effective_compute(q, {"OQ1": {"status": "open"}}, {"OQ1"}) == provisional


def test_boris_projects_read_as_paired_behaviour_intervals(tmp_path: Path):
    project = {
        "project_format_version": "7.0",
        "behaviors_conf": {
            "0": {"code": "run", "type": "State event"},
            "1": {"code": "peck", "type": "Point event"},
        },
        "observations": {
            "obs_A": {
                "events": [  # deliberately out of time order
                    [5.0, "", "run", "", ""],
                    [1.0, "", "run", "", ""],
                    [3.0, "", "peck", "", ""],
                    [8.0, "", "run", "", ""],  # a start with no stop
                ]
            },
            "obs_B_second_rater": {"events": [[2.0, "", "run", "", ""], [2.5, "", "run", "", ""]]},
            "other": {"events": [[0.0, "", "run", "", ""], [9.0, "", "run", "", ""]]},
        },
    }
    (tmp_path / "p.boris").write_text(json.dumps(project), encoding="utf-8")
    config = {"tables": {"ev": {"kind": "boris", "file": "p.boris", "observations": "obs_.*"}}}
    src = xp16lib.FileRowSource(tmp_path, config)
    table = src.table("ev")
    got = [
        (r.values["Observation id"], r.values["Behavior"], r.values["Duration (s)"])
        for r in table.rows
    ]
    assert got == [
        ("obs_A", "run", 4),  # 1.0 -> 5.0, paired in time order
        ("obs_A", "peck", 0),  # a point event is an interval of length 0
        ("obs_A", "run", None),  # 8.0 never stopped: kept, not closed by assumption
        ("obs_B_second_rater", "run", 0.5),
    ]
    total = xp16lib.evaluate(
        src,
        config,
        {
            "op": "sum",
            "table": "ev",
            "column": "Duration (s)",
            "where": [
                {"col": "Observation id", "value": "obs_A"},
                {"col": "Behavior", "value": "run"},
                {"col": "Duration (s)", "op": "numeric"},
            ],
        },
    )
    assert total.answer == 4 and total.sources[0]["range"].startswith("intervals ")


@pytest.fixture
def boris_package(tmp_path: Path) -> Path:
    """A package holding one synthetic BORIS project, two raters' observations."""
    origin = tmp_path / "origin"
    origin.mkdir()
    project = {
        "project_format_version": "7.0",
        "project_name": "synthetic",
        "behaviors_conf": {"0": {"code": "run", "type": "State event"}},
        "subjects_conf": {},
        "observations": {
            "pos_1": {
                "events": [
                    [1.0, "", "run", "", ""],
                    [4.5, "", "run", "", ""],
                    [6.0, "", "run", "", ""],
                    [7.0, "", "run", "", ""],
                ]
            },  # fmt: skip
            "pos_1_rater2": {"events": [[1.5, "", "run", "", ""], [4.0, "", "run", "", ""]]},
        },
    }
    (origin / "p.boris").write_text(json.dumps(project), encoding="utf-8")
    pkg = tmp_path / "pkg"
    (pkg / "config").mkdir(parents=True)
    assert _run(freeze, ["--source", str(origin), "--package", str(pkg)]) == 0
    (pkg / "config" / "tables.yaml").write_text(
        yaml.safe_dump(
            {"tables": {"ev": {"kind": "boris", "file": "p.boris", "service_table": "p.boris"}}}
        ),
        encoding="utf-8",
    )

    def total(obs):
        where = [{"col": "Observation id", "value": obs}, {"col": "Behavior", "value": "run"}]
        return {"op": "sum", "table": "ev", "column": "Duration (s)", "where": where}

    spec = {
        "questions": [
            {
                "id": "Q-raters",
                "category": "consistency",
                "question": "run time per rater?",
                "answer_type": "object",
                "requires": ["boris_project"],
                "compute": {
                    "op": "bundle",
                    "parts": {"a": total("pos_1"), "b": total("pos_1_rater2")},
                },
                "score": {
                    "type": "object",
                    "fields": {
                        "a": {"type": "number", "abs": 0.001},
                        "b": {"type": "number", "abs": 0.001},
                    },
                },  # fmt: skip
            }
        ]
    }
    (pkg / "config" / "questions.spec.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    (pkg / "config" / "conditions.json").write_text(
        json.dumps(
            {
                # one shared key for every condition: the BORIS intervals table
                "service_tables": {"ev": "p.boris#intervals"},
                "conditions": {"plain": {"output": "ingest_plain"}},
            }
        ),
        encoding="utf-8",
    )
    assert _run(builder, ["--package", str(pkg)]) == 0
    return pkg


def test_boris_intervals_through_the_service_match_the_gold(boris_package: Path):
    gold = yaml.safe_load((boris_package / "gold" / "questions.yaml").read_text("utf-8"))
    assert gold["questions"][0]["expected"] == {"a": 4.5, "b": 2.5}
    ingest_step.ingest(boris_package, "plain")
    record = json.loads((boris_package / "ingest_plain" / "condition.json").read_text("utf-8"))
    assert record["service_tables"] == {"ev": "p.boris#intervals"}  # the shared key
    out = boris_package / "b.json"
    argv = ["--package", str(boris_package), "--ingest", str(boris_package / "ingest_plain")]
    assert _run(baseline, [*argv, "--out", str(out)]) == 0
    report = json.loads(out.read_text("utf-8"))
    assert report["questions"][0]["retrieval"]["outcome"] == "pass"
    assert report["column_maps"]["ev"]["matching"] == "by name (BORIS table)"


def test_the_scorer_checks_a_boris_citation_the_way_the_service_locates_rows(
    boris_package: Path,
):
    checksums = json.loads((boris_package / "checksums.json").read_text("utf-8"))
    sha = checksums["files"][0]["sha256"]
    ctx = score.CitationContext(checksums, boris_package / "source")
    q = {"id": "Q", "category": "c", "answer_type": "object", "expected": {}, "requires": []}

    def cite(**loc):
        src = {"file": "p.boris", "sha256": sha, **loc}
        return score.citation_status({"answer": {}, "sources": [src]}, q, ctx)["status"]

    assert cite(observation_id="pos_1", start_event_index=0, stop_event_index=1) == "verified"
    assert cite(observation_id="pos_1", start_event_index=2, stop_event_index=9) == "invalid"
    assert cite(observation_id="nope", start_event_index=0, stop_event_index=1) == "invalid"
