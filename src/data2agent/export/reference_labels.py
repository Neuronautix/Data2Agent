"""Approved reference labels from a FAIR-VCG dataspace -- FOR SCORING TOOLS ONLY.

    WARNING: HOLDOUT ANSWER PROTECTION.
    This module reads ``annotations/curated/``: the human-approved answers the
    benchmark scores against. It exists for a *scorer* that compares an agent's
    output with those answers, after the agent has finished. It must NEVER be
    reachable from anything an agent under evaluation can call or read: not an MCP
    tool, not a prompt, not a log, not a fixture. The dataspace's
    ``docs/annotation-guidelines.md`` ("Holdout Answer Protection") states that
    leaking labels, scores or rationale text for a holdout family invalidates the
    benchmark for that family.

Nothing outside ``data2agent.export`` imports this package (``tests/test_layering.py``
and ``tests/export`` both assert it), and it is deliberately not re-exported from
``data2agent.export``. Keep it that way.

What it returns is deliberately narrow: only records under ``annotations/curated/``
whose ``review.status`` is ``approved``. Anything under ``annotations/proposed/``
(including every agent proposal, whatever its status) and every ``deprecated``
record is ignored. It fails loudly rather than guessing when the dataspace is
inconsistent.
"""

from __future__ import annotations

import json
from pathlib import Path

from .annotations import AnnotationExportError

_ALLOWED_LABELS = frozenset(
    {"conformant", "non_conformant", "partial", "not_applicable", "insufficient_evidence"}
)


class ReferenceLabelError(AnnotationExportError):
    """The curated annotations are unreadable or contradict each other."""


def read_reference_labels(dataspace_root: Path | str, family: str, variant: str) -> dict[str, str]:
    """``criterion id -> label`` from the approved curated records of one family/variant.

    Only ``annotations/curated/`` is read, and only records with ``review.status ==
    "approved"`` for exactly this ``dataset_family_id`` and ``variant``.

    Raises :class:`ReferenceLabelError` when

    * a file under ``annotations/curated/`` cannot be parsed (a corrupt answer key must
      not be silently thinner);
    * an approved record is an ``agent_proposal`` (the dataspace forbids it; such a
      record must never become an answer);
    * two approved records give one criterion different labels. The result is keyed by
      criterion only, so a criterion legitimately judged differently on several targets
      (per-field records) also raises: a target-aware reader is needed for those.

    SCORING TOOLS ONLY -- see the module docstring.
    """
    curated = Path(dataspace_root) / "annotations" / "curated"
    if not curated.is_dir():
        raise ReferenceLabelError(f"{curated} does not exist")
    labels: dict[str, str] = {}
    origin: dict[str, str] = {}
    for path in sorted(curated.rglob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ReferenceLabelError(f"{path} cannot be read as JSON: {error}") from None
        if not isinstance(record, dict):
            raise ReferenceLabelError(f"{path} is not an annotation record")
        review = record.get("review") or {}
        if review.get("status") != "approved":
            continue
        dataset = record.get("dataset") or {}
        if dataset.get("dataset_family_id") != family or dataset.get("variant") != variant:
            continue
        method = (record.get("provenance") or {}).get("method")
        if method == "agent_proposal":
            raise ReferenceLabelError(
                f"{path.name} is an approved agent_proposal, which the dataspace forbids; "
                "refusing to treat it as an answer"
            )
        criterion = (record.get("criterion") or {}).get("id")
        label = (record.get("judgement") or {}).get("label")
        if not isinstance(criterion, str) or not criterion or label not in _ALLOWED_LABELS:
            raise ReferenceLabelError(
                f"{path.name} lacks a criterion id or a valid judgement label"
            )
        if criterion in labels and labels[criterion] != label:
            raise ReferenceLabelError(
                f"approved records disagree on criterion {criterion!r}: "
                f"{origin[criterion]} says {labels[criterion]!r}, {path.name} says {label!r}"
            )
        labels[criterion] = label
        origin.setdefault(criterion, path.name)
    return dict(sorted(labels.items()))
