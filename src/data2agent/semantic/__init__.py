"""Optional semantic layer (disabled by default): runtime ontology constraints and mapping records.

Nothing here runs unless asked for. ``ingest`` never imports it; it never imports the
FAIR profiles or the MCP server. See ``docs/semantic-layer.md``.
"""

from .constraints import (
    ENGINE_VERSION,
    MappingPolicy,
    OntologyRuntime,
    OntologySnapshot,
    SemanticError,
    VocabularyUnavailable,
    get_mapping_constraints,
    get_term_context,
    search_ontology_terms,
    validate_semantic_mapping,
)

__all__ = [
    "ENGINE_VERSION",
    "MappingPolicy",
    "OntologyRuntime",
    "OntologySnapshot",
    "SemanticError",
    "VocabularyUnavailable",
    "get_mapping_constraints",
    "get_term_context",
    "search_ontology_terms",
    "validate_semantic_mapping",
]
