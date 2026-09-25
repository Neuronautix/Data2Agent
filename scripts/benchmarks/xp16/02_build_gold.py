"""Build the XP16 gold standard from the frozen source, independently of Data2Agent.

Public and data-free. Everything dataset-specific is read from the package:

    <pkg>/checksums.json                 from 01_freeze.py
    <pkg>/config/tables.yaml             declared tables (local, git-ignored)
    <pkg>/config/questions.spec.yaml     questions + gold computations (local)
    <pkg>/config/audit_facts.spec.yaml   optional: facts the audit cites (local)
    <pkg>/adjudication/decisions.yaml    owner questions and answers (local)

and written to:

    <pkg>/gold/questions.yaml            expected answers, citations, computations
    <pkg>/gold/audit_facts.yaml          evaluated audit facts (if a spec exists)

    python scripts/benchmarks/xp16/02_build_gold.py --package <pkg>
    python scripts/benchmarks/xp16/02_build_gold.py --package <pkg> --check

``--check`` rebuilds into memory and exits non-zero if the files on disk differ,
which is how byte-identical reproducibility is verified rather than asserted.

Rules the builder enforces:

* The source is verified against checksums.json before anything is read, and
  again afterwards. A drifted snapshot aborts the build.
* Tables are read with openpyxl / csv only (xp16lib.FileRowSource). The system
  under test is never imported.
* A question blocked by an open owner question gets ``expected: PENDING-<ids>``.
  Its computation, if any, is still evaluated and kept apart as
  ``provisional_answer`` so the owner can see what the answer would become.
* Once answered, the question's ``on_answer: {<decision>: {<answer>: outcome}}``
  decides what happens -- ``compute`` (the hypothesis held: its computation
  becomes gold), ``abstain`` (e.g. for "no" or "unknown": the correct reply is
  "cannot be determined"), or an alternative computation ``{op: ...}``. The
  answer key is the decision's ``answer_key`` or its normalised ``answer``. An
  answer with no declared outcome fails the build; nothing defaults.
* Nothing is written with a timestamp, so reruns are byte-identical.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from xp16lib import (
    ABSTAIN,
    OPS,
    PENDING_PREFIX,
    FileRowSource,
    GoldError,
    dump_yaml,
    evaluate,
    load_yaml,
    round_num,
    sha256_file,
    to_number,
)

SCHEMA_VERSION = 1
ANSWER_TYPES = frozenset(
    {"integer", "number", "string", "exact", "boolean", "set", "table", "object", "abstain"}
)
CATEGORIES = frozenset(
    {"inventory", "retrieval", "filtering", "aggregation", "join", "consistency", "abstention"}
)
# The closed capability vocabulary. A tag outside it is a typo until proven
# otherwise, and a typo would silently drop a question out of every breakdown.
CAPABILITIES = frozenset(
    {
        "read_rows",
        "filter",
        "aggregate",
        "join",
        "header_detection",
        "multi_table_sheet",
        "duplicate_headers",
        "boris_tsv_preamble",
        "boris_project",
        "crosswalk",
        "unit_aggregation",
        "code_mapping",
        "cross_file_consistency",
        "formula_audit",
        "version_diff",
        "abstention",
    }
)


def verify_source(pkg: Path) -> dict[str, Any]:
    checksums = json.loads((pkg / "checksums.json").read_text(encoding="utf-8"))
    src = pkg / "source"
    bad = [
        f["path"]
        for f in checksums["files"]
        if not (src / f["path"]).is_file() or sha256_file(src / f["path"]) != f["sha256"]
    ]
    on_disk = {p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file()}
    extra = sorted(on_disk - {f["path"] for f in checksums["files"]})
    if bad or extra:
        sys.exit(f"source drift: changed/missing {bad}, unexpected {extra}")
    return checksums


def open_decisions(pkg: Path) -> tuple[dict[str, Any], set[str]]:
    path = pkg / "adjudication" / "decisions.yaml"
    if not path.exists():
        return {}, set()
    doc = load_yaml(path) or {}
    items = {q["id"]: q for q in doc.get("questions", [])}
    return items, {qid for qid, q in items.items() if q.get("status") != "answered"}


# What an owner answer does to a blocked question is declared per question, per
# decision, per answer -- never inferred from the fact that it was answered. An
# answer of "no" must not promote a computation that assumed "yes", and an
# owner-confirmed "unknown" makes "cannot be determined" the correct reply.
OUTCOME_COMPUTE = "compute"  # the question's own `compute:` (the hypothesis held)
OUTCOME_ABSTAIN = frozenset({"abstain", "unanswerable"})


def answer_key(decision: dict[str, Any]) -> str:
    """The key an answered decision is looked up under in `on_answer`.

    `answer_key` when the owner's prose answer needs a short handle, otherwise
    the answer itself, normalised (so `Yes` and `yes` are one key).
    """
    raw = decision.get("answer_key")
    if raw is None:
        raw = decision.get("answer")
    return " ".join(str(raw).split()).casefold()


def _outcome_problems(qid: str, q: dict[str, Any]) -> list[str]:
    problems = []
    on_answer = q.get("on_answer") or {}
    for oq, mapping in on_answer.items():
        if oq not in q.get("blocked_by", []):
            problems.append(f"{qid}: on_answer names {oq}, which is not in blocked_by")
        if not isinstance(mapping, dict) or not mapping:
            problems.append(f"{qid}: on_answer.{oq} must map answers to outcomes")
            continue
        for key, outcome in mapping.items():
            if isinstance(outcome, dict):
                if outcome.get("op") not in OPS:
                    problems.append(
                        f"{qid}: on_answer.{oq}.{key}: unknown op {outcome.get('op')!r}"
                    )
            elif outcome == OUTCOME_COMPUTE:
                if "compute" not in q:
                    problems.append(f"{qid}: on_answer.{oq}.{key} = compute, but no compute:")
            elif outcome not in OUTCOME_ABSTAIN:
                problems.append(f"{qid}: on_answer.{oq}.{key}: unknown outcome {outcome!r}")
    return problems


def resolve_blocked(
    q: dict[str, Any], decisions: dict[str, Any]
) -> tuple[str | dict[str, Any], dict[str, str]]:
    """Outcome of a question whose blocking decisions are all answered.

    Returns (outcome, {decision id: answer key}). An answer the question does not
    map is an error, so a new owner answer can never silently fall through to a
    default -- in particular not to the provisional computation.
    """
    keys: dict[str, str] = {}
    outcomes: list[str | dict[str, Any]] = []
    on_answer = q.get("on_answer") or {}
    for oq in q.get("blocked_by", []):
        key = answer_key(decisions[oq])
        keys[oq] = key
        mapping = {
            " ".join(str(k).split()).casefold(): v for k, v in (on_answer.get(oq) or {}).items()
        }
        if key not in mapping:
            raise GoldError(
                f"{oq} is answered {key!r}, but {q['id']} declares no outcome for that answer "
                f"(on_answer.{oq} covers {sorted(mapping) or 'nothing'})"
            )
        outcomes.append(mapping[key])
    if any(o in OUTCOME_ABSTAIN for o in outcomes if isinstance(o, str)):
        return "abstain", keys
    alternatives = [o for o in outcomes if isinstance(o, dict)]
    if len(alternatives) > 1:
        raise GoldError(f"{q['id']}: several answers each select an alternative computation")
    if alternatives:
        return alternatives[0], keys
    return OUTCOME_COMPUTE, keys


def validate(spec: dict[str, Any], decisions: dict[str, Any]) -> list[str]:
    problems = []
    seen = set()
    for q in spec.get("questions", []):
        qid = q.get("id", "?")
        if qid in seen:
            problems.append(f"{qid}: duplicate id")
        seen.add(qid)
        if q.get("answer_type") not in ANSWER_TYPES:
            problems.append(f"{qid}: answer_type {q.get('answer_type')!r}")
        if q.get("category") not in CATEGORIES:
            problems.append(f"{qid}: category {q.get('category')!r}")
        unknown = set(q.get("requires", [])) - CAPABILITIES
        if unknown:
            problems.append(f"{qid}: unknown capability tag(s) {sorted(unknown)}")
        for oq in q.get("blocked_by", []) + q.get("related_owner_questions", []):
            if oq not in decisions:
                problems.append(f"{qid}: references owner question {oq} not in decisions.yaml")
        if "compute" in q and q["compute"].get("op") not in OPS:
            problems.append(f"{qid}: unknown op {q['compute'].get('op')!r}")
        problems.extend(_outcome_problems(qid, q))
    return problems


def build(pkg: Path) -> dict[str, str]:
    checksums = verify_source(pkg)
    config = load_yaml(pkg / "config" / "tables.yaml")
    spec = load_yaml(pkg / "config" / "questions.spec.yaml")
    decisions, open_ids = open_decisions(pkg)
    problems = validate(spec, decisions)
    if problems:
        sys.exit("question spec is invalid:\n  " + "\n  ".join(problems))

    src = FileRowSource(pkg / "source", config)
    out_questions = []
    for q in spec["questions"]:
        record: dict[str, Any] = {
            "id": q["id"],
            "category": q["category"],
            "question": " ".join(str(q["question"]).split()),
            "answer_type": q["answer_type"],
            "requires": list(q.get("requires", [])),
            "blocked_by": list(q.get("blocked_by", [])),
        }
        if q.get("related_owner_questions"):
            record["related_owner_questions"] = list(q["related_owner_questions"])
        blocking_open = [b for b in q.get("blocked_by", []) if b in open_ids]
        outcome: str | dict[str, Any] = OUTCOME_COMPUTE
        try:
            result = evaluate(src, config, q["compute"]) if "compute" in q else None
            if q.get("blocked_by") and not blocking_open:
                outcome, keys = resolve_blocked(q, decisions)
                record["resolved_by"] = keys
                if isinstance(outcome, dict):
                    result = evaluate(src, config, outcome)
        except (GoldError, KeyError, ValueError) as exc:
            sys.exit(f"{q['id']}: {exc}")

        if outcome == "abstain":
            record["status"] = "abstain"
            record["expected"] = ABSTAIN
            record["abstain_reason"] = (
                "owner answer(s) "
                + ", ".join(f"{k}={v!r}" for k, v in record["resolved_by"].items())
                + " leave the question undeterminable"
            )
        elif blocking_open:
            record["status"] = "pending"
            record["expected"] = PENDING_PREFIX + "+".join(blocking_open)
            if result is not None:
                record["provisional_answer"] = result.answer
                record["provisional_note"] = (
                    "computed under the hypothesis the blocking owner question asks about; "
                    "NOT gold until it is answered"
                )
        elif result is not None and result.answer == ABSTAIN:
            record["status"] = "abstain"
            record["expected"] = ABSTAIN
        else:
            record["status"] = "answerable"
            record["expected"] = _typed(result.answer, q["answer_type"]) if result else None
        if q.get("score"):
            record["score"] = q["score"]
        elif q["answer_type"] == "number":
            record["tolerance"] = q.get("tolerance", {"abs": 0.001})
        if result is not None:
            record["sources"] = result.sources
            record["computation"] = result.computation
        if q.get("notes"):
            record["notes"] = " ".join(str(q["notes"]).split())
        out_questions.append(record)

    header = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": checksums["dataset_id"],
        "generator": {
            "script": "scripts/benchmarks/xp16/02_build_gold.py",
            "script_sha256": sha256_file(Path(__file__)),
            "lib_sha256": sha256_file(Path(__file__).with_name("xp16lib.py")),
            "tables_config_sha256": sha256_file(pkg / "config" / "tables.yaml"),
            "questions_spec_sha256": sha256_file(pkg / "config" / "questions.spec.yaml"),
            "decisions_sha256": (
                sha256_file(pkg / "adjudication" / "decisions.yaml")
                if (pkg / "adjudication" / "decisions.yaml").exists()
                else None
            ),
        },
        "open_owner_questions": sorted(open_ids),
        "counts": _counts(out_questions),
    }
    files = {
        "gold/questions.yaml": "# GENERATED by 02_build_gold.py -- do not edit; edit the spec.\n"
        + dump_yaml({**header, "questions": out_questions}),
    }

    facts_spec = pkg / "config" / "audit_facts.spec.yaml"
    if facts_spec.exists():
        facts = []
        for f in (load_yaml(facts_spec) or {}).get("facts", []):
            try:
                r = evaluate(src, config, f["compute"])
            except (GoldError, KeyError, ValueError) as exc:
                sys.exit(f"audit fact {f['id']}: {exc}")
            facts.append(
                {
                    "id": f["id"],
                    "statement": " ".join(str(f["statement"]).split()),
                    "result": r.answer,
                    "sources": r.sources,
                    "computation": r.computation,
                }
            )
        files["gold/audit_facts.yaml"] = (
            "# GENERATED by 02_build_gold.py -- do not edit.\n"
            + dump_yaml({"dataset_id": checksums["dataset_id"], "facts": facts})
        )

    verify_source(pkg)  # nothing may have moved while we read
    return files


def _typed(value: Any, answer_type: str) -> Any:
    """Delimited exports yield text; a numeric question's gold is a number.

    Only a clean numeric string is converted. Anything else is left exactly as
    read, so a text marker in a numeric cell stays visible instead of vanishing.
    """
    if answer_type in {"number", "integer"} and isinstance(value, str):
        x = to_number(value)
        if x is not None:
            return round_num(x)
    return value


def _counts(questions: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, dict[str, int]] = {"category": {}, "status": {}, "requires": {}}
    for q in questions:
        by["category"][q["category"]] = by["category"].get(q["category"], 0) + 1
        by["status"][q["status"]] = by["status"].get(q["status"], 0) + 1
        for cap in q["requires"]:
            by["requires"][cap] = by["requires"].get(cap, 0) + 1
    return {
        "questions": len(questions),
        **{k: dict(sorted(v.items())) for k, v in by.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="verify files on disk are current")
    args = parser.parse_args()
    pkg = args.package.resolve()
    files = build(pkg)
    if args.check:
        stale = [
            rel
            for rel, text in files.items()
            if not (pkg / rel).exists() or (pkg / rel).read_bytes() != text.encode("utf-8")
        ]
        if stale:
            print("STALE:", ", ".join(stale))
            return 1
        print("gold is byte-identical to a fresh build")
        return 0
    for rel, text in files.items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        print("wrote", target)
    counts = load_yaml(pkg / "gold" / "questions.yaml")["counts"]
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
