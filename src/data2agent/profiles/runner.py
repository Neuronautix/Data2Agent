"""Running a profile's deterministic checks.

The runner is the enforcement point for a profile's own rules. It refuses an
outcome the rule did not permit, an outcome with no evidence, and a `fail` or
`unknown` with no rationale. A check that tries to bend its rule fails the run
rather than quietly widening what the profile is allowed to say.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .loader import ProfileError
from .model import (
    NOT_APPLICABLE,
    UNKNOWN,
    Assessment,
    CheckOutcome,
    Profile,
    ProfileContext,
    Rule,
    RuleResult,
)

CheckImplementation = Callable[[Rule, ProfileContext], CheckOutcome]


def run(
    profile: Profile,
    context: ProfileContext,
    checks: dict[str, CheckImplementation],
    *,
    generator: dict[str, Any],
    rule_id: str | None = None,
) -> Assessment:
    """Run every rule in ``profile``, or just one."""
    rules = [profile.rule(rule_id)] if rule_id else list(profile.rules)
    results = [_run_rule(rule, context, checks) for rule in rules]
    return Assessment(
        dataset_id=context.manifest.get("dataset_id", ""),
        profile=profile,
        generator=generator,
        results=results,
    )


def _run_rule(
    rule: Rule, context: ProfileContext, checks: dict[str, CheckImplementation]
) -> RuleResult:
    if not rule.implemented:
        # Preserved as unknown rather than skipped. A skipped rule disappears
        # from the denominator; an unknown one stays visible and countable.
        return RuleResult(
            rule_id=rule.id,
            principle=rule.principle,
            result=UNKNOWN,
            rationale=rule.not_implemented_reason,
            evidence=[
                {
                    "check": rule.check_type,
                    "result": "not-implemented",
                    "detail": rule.not_implemented_reason,
                }
            ],
        )

    implementation = checks.get(rule.check_type)
    if implementation is None:
        raise ProfileError(
            f"rule '{rule.id}' declares check type '{rule.check_type}', which this "
            f"profile does not implement; mark the rule 'implemented: false' or add the check"
        )

    outcome = implementation(rule, context)
    _validate(rule, outcome)
    return RuleResult(
        rule_id=rule.id,
        principle=rule.principle,
        result=outcome.result,
        evidence=list(outcome.evidence),
        rationale=outcome.rationale,
        inferred=False,
    )


def _validate(rule: Rule, outcome: CheckOutcome) -> None:
    if outcome.result not in rule.allowed_results:
        raise ProfileError(
            f"check for '{rule.id}' returned '{outcome.result}', which the rule does "
            f"not permit (allowed: {list(rule.allowed_results)})"
        )
    if rule.evidence_required and not outcome.evidence:
        raise ProfileError(
            f"check for '{rule.id}' returned '{outcome.result}' with no evidence; every "
            f"result must say what was looked at, 'unknown' and 'not_applicable' included"
        )
    if outcome.result in rule.rationale_required_for and not outcome.rationale.strip():
        raise ProfileError(
            f"check for '{rule.id}' returned '{outcome.result}' with no rationale, which "
            f"the rule requires for that outcome"
        )
    if outcome.result == NOT_APPLICABLE and not outcome.rationale.strip():
        raise ProfileError(
            f"check for '{rule.id}' returned 'not_applicable' without saying why it "
            f"does not apply, which is indistinguishable from a silent skip"
        )
