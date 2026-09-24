"""Evidence-bearing cross-table relationship analysis."""

RELATIONSHIPS_FILENAME = "relationships.json"

from .analysis import (
    CARDINALITIES,
    RELATIONSHIP_VERSION,
    STATUSES,
    assess_relationship,
    candidate_key_specs,
    declaration_key,
    stable_relationship_id,
)

__all__ = [
    "CARDINALITIES",
    "RELATIONSHIPS_FILENAME",
    "RELATIONSHIP_VERSION",
    "STATUSES",
    "assess_relationship",
    "candidate_key_specs",
    "declaration_key",
    "stable_relationship_id",
]
