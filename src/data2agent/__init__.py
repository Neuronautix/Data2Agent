"""Data2Agent: immutable datasets, deterministic ingestion, evidence-bound answers.

The package is layered so that each layer can be tested — and replaced —
independently:

``data2agent.ingest``
    Deterministic, LLM-free scanning of a dataset directory into a manifest.
``data2agent.evidence``
    The claim -> evidence -> source file -> checksum chain.
``data2agent.mcp``
    A host-agnostic service plus a thin MCP binding over an ingested dataset.

Nothing in the core knows about FAIR, about agents, or about a particular
coding-agent host. Those sit on top.
"""

__version__ = "0.1.0"

MANIFEST_VERSION = "0.5.0"
EVIDENCE_VERSION = "0.3.0"

__all__ = ["__version__", "MANIFEST_VERSION", "EVIDENCE_VERSION"]
