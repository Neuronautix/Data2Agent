"""Evidence-bearing cross-table relationship analysis."""

from .analysis import (
    CARDINALITIES,
    RELATIONSHIP_VERSION,
    STATUSES,
    assess_relationship,
    candidate_key_specs,
    declaration_key,
    stable_relationship_id,
)
from .crosswalk import (
    CROSSWALK_FORMAT,
    Crosswalk,
    CrosswalkError,
    load_crosswalk_file,
    parse_crosswalk,
)

RELATIONSHIPS_FILENAME = "relationships.json"

__all__ = [
    "CARDINALITIES",
    "CROSSWALK_FORMAT",
    "Crosswalk",
    "CrosswalkError",
    "RELATIONSHIPS_FILENAME",
    "RELATIONSHIP_VERSION",
    "STATUSES",
    "assess_relationship",
    "candidate_key_specs",
    "declaration_key",
    "load_crosswalk_file",
    "parse_crosswalk",
    "stable_relationship_id",
]
