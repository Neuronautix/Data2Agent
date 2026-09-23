"""Score a FAIR assessment against the XP14 gold standard.

Public: reads the gold file and an assessment, writes a score. Contains no
dataset content, and names nothing but rule ids.

Two questions, deliberately not collapsed into one number:

**Agreement** -- does each verdict match ``expected_result``, the truth for
these bytes? The gold file's own ``scoring_guidance`` insists this is scored
against ``expected_result`` and never against ``v0_1_actual``: where the two
differ, v0.1 has a known gap and a better assessor may legitimately beat it.

**Movement** -- how does each verdict compare with ``v0_1_actual``, what the
deterministic profile returned at freeze? This is what makes progress
measurable. A gap that closes and a gap that reopens both surface here, and
only the second is a regression.

Separately, and more seriously, the gold file names indicators that must stay
``unknown``: resolving an identifier needs a network request, and retrievability
is a property of where a dataset is published. A pass or fail on either means a
fact was inferred from a local snapshot. That is a different kind of failure
from being wrong -- it is the failure this benchmark exists to detect -- so it
is reported on its own line and fails the run by itself, even from an otherwise
perfect score.

Usage
    python scripts/benchmarks/xp14/07_score.py <assessment.json>
    python scripts/benchmarks/xp14/07_score.py <assessment.json> --json out.json
    python scripts/benchmarks/xp14/07_score.py <assessment.json> --gold <path>

Exit status is 0 only when every indicator agrees, nothing regressed against
freeze, and no must-stay-unknown indicator was resolved.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Verdicts a rule may carry. An assessment using anything else is not scored: a
# vocabulary this script does not know is a reason to stop, not to guess.
_VERDICTS = frozenset({"pass", "fail", "unknown", "not_applicable"})

# The gold schema this script understands. Bumping it is a deliberate act.
_GOLD_SCHEMA = 1


def _repo_root() -> Path:
    env = os.environ.get("D2A_REPO")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[3]


REPO = _repo_root()
DEFAULT_GOLD = REPO / "benchmarks" / "xp14_apa" / "gold" / "fair_expected.json"


def load_gold(path: Path) -> dict[str, Any]:
    """Load the gold file, refusing anything whose shape is not the known one.

    Written after a hand-rolled scorer guessed this schema, keyed on a field the
    file does not carry, and reported a confident 0/21 with every indicator
    marked as differing. A scorer that cannot find its ground truth must say so:
    silently scoring against nothing is worse than not scoring, because the
    number that comes out looks exactly like a measurement.
    """
    if not path.exists():
        sys.exit(f"gold file not found: {path}\n(set $D2A_REPO, or pass --gold)")
    try:
        gold = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"gold file is not valid JSON: {path}: {exc}")

    version = gold.get("schema_version")
    if version != _GOLD_SCHEMA:
        sys.exit(
            f"gold schema_version is {version!r}, and this script understands "
            f"{_GOLD_SCHEMA}. Refusing to score against a shape it may misread."
        )
    indicators = gold.get("indicators")
    if not isinstance(indicators, list) or not indicators:
        sys.exit("gold file has no 'indicators' list; there is nothing to score against")
    for index, indicator in enumerate(indicators):
        for field in ("id", "expected_result"):
            if field not in indicator:
                sys.exit(f"gold indicator {index} has no {field!r}")
        if indicator["expected_result"] not in _VERDICTS:
            sys.exit(
                f"gold indicator {indicator['id']} expects "
                f"{indicator['expected_result']!r}, which is not a known verdict"
            )
    return gold


def load_assessment(path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    """Return {rule_id: result}, plus whatever the record says about its origin."""
    if not path.exists():
        sys.exit(f"assessment not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"assessment is not valid JSON: {path}: {exc}")

    results = payload.get("results")
    if not isinstance(results, list):
        sys.exit("assessment has no 'results' list")

    verdicts: dict[str, str] = {}
    for item in results:
        rule_id, result = item.get("rule_id"), item.get("result")
        if rule_id is None:
            sys.exit("an assessment result has no 'rule_id'")
        if result not in _VERDICTS:
            sys.exit(f"{rule_id} carries verdict {result!r}, which is not recognised")
        if rule_id in verdicts:
            sys.exit(f"{rule_id} appears twice in the assessment; refusing to pick one")
        verdicts[rule_id] = result

    provenance = {
        "dataset_id": payload.get("dataset_id"),
        "profile": payload.get("profile"),
        "generator": payload.get("generator"),
        "assessment_version": payload.get("assessment_version"),
    }
    return verdicts, provenance


def _freeze_verdict(indicator: dict[str, Any]) -> str | None:
    """The verdict v0.1 returned at freeze, or None where it recorded none.

    ``v0_1_actual`` is prose in places -- "fail, citing ['xlsx']" -- because the
    letter of the verdict was right while the evidence under it was not. Only
    the leading verdict is comparable; the remark is kept for the report rather
    than parsed for meaning.
    """
    raw = indicator.get("v0_1_actual")
    if not isinstance(raw, str):
        return None
    head = raw.split(",", 1)[0].strip()
    return head if head in _VERDICTS else None


def _must_stay_unknown(gold: dict[str, Any]) -> set[str]:
    """Indicators the gold forbids resolving, read from the gold itself.

    Taken by matching known rule ids against
    ``scoring_guidance.unknown_preservation`` rather than hard-coded here. The
    gold file is the authority, and a scorer that keeps its own copy of the rule
    will eventually disagree with the file it is scoring against.
    """
    guidance = gold.get("scoring_guidance")
    text = guidance.get("unknown_preservation", "") if isinstance(guidance, dict) else ""
    known = {indicator["id"] for indicator in gold["indicators"]}
    return {rule_id for rule_id in known if rule_id in text}


def score(gold: dict[str, Any], verdicts: dict[str, str]) -> dict[str, Any]:
    must_stay_unknown = _must_stay_unknown(gold)

    rows: list[dict[str, Any]] = []
    for indicator in gold["indicators"]:
        rule_id = indicator["id"]
        expected = indicator["expected_result"]
        observed = verdicts.get(rule_id)
        at_freeze = _freeze_verdict(indicator)
        # Whether v0.1 was RIGHT at freeze is the gold's judgement, not a
        # comparison of verdict letters, and the two genuinely differ.
        # I1-DATA-FORMATS-OPEN returned 'fail' at freeze and is marked
        # agrees: False, because the verdict was correct while the evidence
        # under it was not -- only the 9 files named .xlsx were evaluated, and
        # the 20 OOXML files named .xls escaped the check. Scoring closure off
        # the letter would call that gap closed the day it was found and hide
        # it the day it was fixed.
        agreed_at_freeze = bool(indicator.get("agrees", True))
        rows.append(
            {
                "rule_id": rule_id,
                "expected": expected,
                "observed": observed,
                "agrees": observed == expected,
                "at_freeze": at_freeze,
                "agreed_at_freeze": agreed_at_freeze,
                "moved": at_freeze is not None and observed is not None and observed != at_freeze,
                "closed": not agreed_at_freeze and observed == expected,
                "regressed": agreed_at_freeze and observed is not None and observed != expected,
                "resolved_an_unknown": rule_id in must_stay_unknown
                and observed is not None
                and observed != "unknown",
                "backlog": indicator.get("backlog"),
            }
        )

    # Rules the assessment reports and the gold does not. Not scored -- the gold
    # is the authority on what is in scope -- but reported, because a profile
    # that has grown a rule since the freeze is a fact about comparability.
    unscored = sorted(set(verdicts) - {row["rule_id"] for row in rows})

    return {
        "rows": rows,
        "unscored": unscored,
        "agreement": sum(1 for row in rows if row["agrees"]),
        "total": len(rows),
        "at_freeze": sum(1 for item in gold["indicators"] if item.get("agrees", True)),
        "missing": [row["rule_id"] for row in rows if row["observed"] is None],
        "closed": [row["rule_id"] for row in rows if row["closed"]],
        "regressed": [row["rule_id"] for row in rows if row["regressed"]],
        "fabricated": [row["rule_id"] for row in rows if row["resolved_an_unknown"]],
    }


def report(result: dict[str, Any], provenance: dict[str, Any], gold: dict[str, Any]) -> None:
    print(f"  gold       : {gold.get('profile')} {gold.get('profile_version')}")
    print(f"  dataset_id : {provenance.get('dataset_id')}")
    if provenance.get("dataset_id") != gold.get("dataset_id"):
        print("               ^ DIFFERENT BYTES from the gold standard; scores are not comparable")
    generator = provenance.get("generator") or {}
    if generator:
        print(f"  generator  : {json.dumps(generator, sort_keys=True)}")
    print()

    header = f"  {'RULE':<32} {'EXPECTED':<15} {'OBSERVED':<15} SINCE FREEZE"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for row in result["rows"]:
        observed = row["observed"] or "MISSING"
        if row["observed"] is None:
            movement = "not reported"
        elif row["moved"]:
            movement = f"{row['at_freeze']} -> {row['observed']}"
        else:
            movement = ""
        if row["resolved_an_unknown"]:
            flag = "   <-- RESOLVED A MUST-STAY-UNKNOWN"
        elif row["regressed"]:
            flag = "   <-- REGRESSED"
        elif not row["agrees"]:
            flag = "   <-- differs"
        else:
            flag = ""
        print(f"  {row['rule_id']:<32} {row['expected']:<15} {observed:<15} {movement}{flag}")

    print()
    print(f"  agreement with gold : {result['agreement']}/{result['total']}")
    print(f"  at freeze           : {result['at_freeze']}/{result['total']}")

    if result["closed"]:
        print("\n  gaps closed since freeze:")
        for row in result["rows"]:
            if not row["closed"]:
                continue
            backlog = f"  ({row['backlog']})" if row["backlog"] else ""
            if row["moved"]:
                print(f"    {row['rule_id']}  {row['at_freeze']} -> {row['observed']}{backlog}")
            else:
                # The verdict letter never moved: v0.1 reached the right answer
                # on the wrong evidence, and the repair is invisible here. Say
                # so, rather than printing 'fail -> fail' as though nothing had
                # happened.
                print(
                    f"    {row['rule_id']}  verdict unchanged ({row['observed']}); "
                    f"the evidence under it was wrong at freeze{backlog}"
                )

    disagreements = [row for row in result["rows"] if not row["agrees"]]
    if disagreements:
        print("\n  remaining disagreements:")
        for row in disagreements:
            print(
                f"    {row['rule_id']}  expected {row['expected']}, "
                f"got {row['observed'] or 'MISSING'}"
            )
            if row["backlog"]:
                print(f"      backlog: {row['backlog']}")

    if result["unscored"]:
        print(f"\n  reported but not in the gold ({len(result['unscored'])}):")
        for rule_id in result["unscored"]:
            print(f"    {rule_id}")

    if result["fabricated"]:
        print("\n  EPISTEMIC FAILURE -- an indicator the gold forbids resolving was resolved:")
        for row in result["rows"]:
            if row["resolved_an_unknown"]:
                print(f"    {row['rule_id']}  returned {row['observed']}, must stay unknown")
        guidance = gold.get("scoring_guidance") or {}
        print(f"    {guidance.get('unknown_preservation', '')}")

    if result["regressed"]:
        print("\n  REGRESSION -- agreed at freeze and does not now:")
        for rule_id in result["regressed"]:
            print(f"    {rule_id}")

    traps = (gold.get("scoring_guidance") or {}).get("fabrication_traps")
    if traps:
        print("\n  not checkable from an assessment alone -- judge these against the")
        print("  agent's own prose when scoring a non-deterministic run:")
        for trap in traps:
            print(f"    - {trap}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("assessment", type=Path, help="assessment.json to score")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD, help="gold fair_expected.json")
    parser.add_argument("--json", type=Path, help="also write the score as JSON")
    args = parser.parse_args()

    gold = load_gold(args.gold)
    verdicts, provenance = load_assessment(args.assessment)
    result = score(gold, verdicts)
    report(result, provenance, gold)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "scored_against": {
                        "dataset_id": gold.get("dataset_id"),
                        "profile": gold.get("profile"),
                        "profile_version": gold.get("profile_version"),
                        "schema_version": gold.get("schema_version"),
                    },
                    "assessment": provenance,
                    "agreement": result["agreement"],
                    "total": result["total"],
                    "at_freeze": result["at_freeze"],
                    "closed": result["closed"],
                    "regressed": result["regressed"],
                    "fabricated": result["fabricated"],
                    "missing": result["missing"],
                    "unscored": result["unscored"],
                    "indicators": result["rows"],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\n  written: {args.json}")

    if result["fabricated"]:
        return 2
    if result["regressed"] or result["missing"]:
        return 1
    return 0 if result["agreement"] == result["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
