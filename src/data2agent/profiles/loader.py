"""Loading and validating a profile's canonical rules.

Rules are authored in YAML because a human maintains them and a reviewer must be
able to read a diff. The loader is strict: an unknown key, a missing field, a
result outside the four allowed outcomes, or a check type with no implementation
is an error at load time, not a surprise at assessment time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import Data2AgentError
from .model import RESULTS, Profile, Rule

PROFILES_DIR = Path(__file__).resolve().parent

_YAML_IMPORT_HINT = (
    "assessment profiles are authored in YAML and need PyYAML: "
    "pip install 'data2agent[fair]'\n"
    "(the deterministic ingest core has no such dependency and works without it)"
)

_REQUIRED_RULE_KEYS = {"id", "principle", "question", "check"}
_ALLOWED_RULE_KEYS = _REQUIRED_RULE_KEYS | {
    "allowed_results",
    "inference_allowed",
    "evidence_required",
    "rationale_required_for",
    "implemented",
    "not_implemented_reason",
    "notes",
}


class ProfileError(Data2AgentError):
    """A profile or one of its rules is malformed."""


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError as error:  # pragma: no cover - depends on optional extra
        raise ImportError(_YAML_IMPORT_HINT) from error
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_profile(profile_id: str = "fair", *, root: Path | None = None) -> Profile:
    """Load a profile and every rule beneath it, validating as we go."""
    base = (root or PROFILES_DIR) / profile_id
    descriptor_path = base / "profile.yaml"
    if not descriptor_path.is_file():
        raise ProfileError(f"no profile descriptor at {descriptor_path}")

    descriptor = _load_yaml(descriptor_path) or {}
    rules_dir = base / "rules"
    if not rules_dir.is_dir():
        raise ProfileError(f"profile '{profile_id}' has no rules directory at {rules_dir}")

    rules: list[Rule] = []
    seen: set[str] = set()
    # Sorted so a profile always loads in the same order, on any filesystem.
    for path in sorted(rules_dir.glob("*.yaml")):
        rule = _parse_rule(_load_yaml(path) or {}, path)
        if rule.id in seen:
            raise ProfileError(f"duplicate rule id '{rule.id}' (second occurrence in {path.name})")
        seen.add(rule.id)
        rules.append(rule)

    if not rules:
        raise ProfileError(f"profile '{profile_id}' defines no rules")

    declared = descriptor.get("rules")
    if declared is not None:
        missing = sorted(set(declared) - seen)
        extra = sorted(seen - set(declared))
        if missing or extra:
            raise ProfileError(
                f"profile.yaml rule list disagrees with rules/: missing {missing}, unlisted {extra}"
            )

    return Profile(
        id=descriptor.get("id", profile_id),
        version=str(descriptor.get("version", "0.0.0")),
        title=descriptor.get("title", profile_id),
        description=descriptor.get("description", ""),
        rules=tuple(sorted(rules, key=lambda rule: (rule.principle, rule.id))),
    )


def _parse_rule(payload: dict[str, Any], path: Path) -> Rule:
    if not isinstance(payload, dict):
        raise ProfileError(f"{path.name}: rule must be a mapping")

    unknown = set(payload) - _ALLOWED_RULE_KEYS
    if unknown:
        raise ProfileError(f"{path.name}: unknown key(s) {sorted(unknown)}")
    missing = _REQUIRED_RULE_KEYS - set(payload)
    if missing:
        raise ProfileError(f"{path.name}: missing required key(s) {sorted(missing)}")

    check = payload["check"]
    if not isinstance(check, dict) or "type" not in check:
        raise ProfileError(f"{path.name}: 'check' must be a mapping with a 'type'")

    allowed = tuple(payload.get("allowed_results", RESULTS))
    invalid = [result for result in allowed if result not in RESULTS]
    if invalid:
        raise ProfileError(
            f"{path.name}: allowed_results contains {invalid}; permitted: {list(RESULTS)}"
        )
    if "unknown" not in allowed:
        # A rule that cannot say "unknown" is a rule that will eventually invent
        # an answer, so the loader refuses to accept one.
        raise ProfileError(
            f"{path.name}: allowed_results must include 'unknown'; a rule that cannot "
            f"report uncertainty will manufacture certainty instead"
        )

    implemented = bool(payload.get("implemented", True))
    reason = payload.get("not_implemented_reason", "")
    if not implemented and not reason:
        raise ProfileError(f"{path.name}: an unimplemented rule must give not_implemented_reason")

    inference_allowed = bool(payload.get("inference_allowed", False))
    if inference_allowed and not payload.get("notes"):
        raise ProfileError(
            f"{path.name}: inference_allowed: true requires a written justification in 'notes'"
        )

    return Rule(
        id=str(payload["id"]),
        principle=str(payload["principle"]),
        question=str(payload["question"]).strip(),
        check_type=str(check["type"]),
        check_inputs=tuple(check.get("inputs", []) or []),
        allowed_results=allowed,
        inference_allowed=inference_allowed,
        evidence_required=bool(payload.get("evidence_required", True)),
        rationale_required_for=tuple(payload.get("rationale_required_for", ("fail", "unknown"))),
        implemented=implemented,
        not_implemented_reason=str(reason),
        notes=str(payload.get("notes", "")).strip(),
    )
