"""The canonical rule registry, and the guarantees the loader enforces on it."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="profiles need the 'fair' extra")

from data2agent.profiles.fair import CHECKS  # noqa: E402
from data2agent.profiles.loader import ProfileError, load_profile  # noqa: E402
from data2agent.profiles.model import RESULTS  # noqa: E402


@pytest.fixture(scope="module")
def profile():
    return load_profile("fair")


def test_the_profile_covers_all_four_principles(profile):
    letters = {rule.principle[0] for rule in profile.rules}
    assert letters == {"F", "A", "I", "R"}


def test_every_rule_has_a_check_that_exists_or_is_declared_unimplemented(profile):
    for rule in profile.rules:
        if rule.implemented:
            assert rule.check_type in CHECKS, (
                f"{rule.id}: no implementation for '{rule.check_type}'"
            )
        else:
            assert rule.not_implemented_reason, f"{rule.id}: unimplemented with no reason"


def test_no_rule_permits_inference(profile):
    """The profile's whole value rests on this. Any exception needs a written case."""
    for rule in profile.rules:
        assert rule.inference_allowed is False, f"{rule.id} allows inference"


def test_every_rule_can_report_unknown(profile):
    for rule in profile.rules:
        assert "unknown" in rule.allowed_results
        assert set(rule.allowed_results) <= set(RESULTS)


def test_every_rule_requires_evidence(profile):
    for rule in profile.rules:
        assert rule.evidence_required, f"{rule.id} would allow an unsourced verdict"


def test_rule_ids_are_unique_and_the_descriptor_agrees(profile):
    ids = [rule.id for rule in profile.rules]
    assert len(ids) == len(set(ids))
    assert len(ids) == 12


def test_rules_load_in_a_stable_order(profile):
    assert list(profile.rules) == sorted(profile.rules, key=lambda rule: (rule.principle, rule.id))


def _write_rule(tmp_path: Path, body: str) -> Path:
    base = tmp_path / "probe"
    (base / "rules").mkdir(parents=True)
    (base / "profile.yaml").write_text("id: probe\nversion: 0.0.1\n", encoding="utf-8")
    (base / "rules" / "X1-RULE.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    return tmp_path


def test_loader_rejects_a_rule_that_cannot_say_unknown(tmp_path: Path):
    root = _write_rule(
        tmp_path,
        """
        id: X1-RULE
        principle: F1
        question: Does the thing hold?
        check: {type: metadata_presence}
        allowed_results: [pass, fail]
        """,
    )
    with pytest.raises(ProfileError, match="must include 'unknown'"):
        load_profile("probe", root=root)


def test_loader_rejects_inference_without_a_written_justification(tmp_path: Path):
    root = _write_rule(
        tmp_path,
        """
        id: X1-RULE
        principle: F1
        question: Does the thing hold?
        check: {type: metadata_presence}
        inference_allowed: true
        """,
    )
    with pytest.raises(ProfileError, match="requires a written justification"):
        load_profile("probe", root=root)


def test_loader_rejects_an_unimplemented_rule_with_no_reason(tmp_path: Path):
    root = _write_rule(
        tmp_path,
        """
        id: X1-RULE
        principle: F1
        question: Does the thing hold?
        check: {type: retrieval_protocol}
        implemented: false
        """,
    )
    with pytest.raises(ProfileError, match="not_implemented_reason"):
        load_profile("probe", root=root)


def test_loader_rejects_an_unknown_key(tmp_path: Path):
    root = _write_rule(
        tmp_path,
        """
        id: X1-RULE
        principle: F1
        question: Does the thing hold?
        check: {type: metadata_presence}
        severity: critical
        """,
    )
    with pytest.raises(ProfileError, match="unknown key"):
        load_profile("probe", root=root)


def test_loader_rejects_a_descriptor_that_disagrees_with_the_rules(tmp_path: Path):
    root = _write_rule(
        tmp_path,
        """
        id: X1-RULE
        principle: F1
        question: Does the thing hold?
        check: {type: metadata_presence}
        """,
    )
    (root / "probe" / "profile.yaml").write_text(
        "id: probe\nversion: 0.0.1\nrules: [X1-RULE, X2-GHOST]\n", encoding="utf-8"
    )
    with pytest.raises(ProfileError, match="disagrees"):
        load_profile("probe", root=root)
