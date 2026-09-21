"""Benchmark modes.

The experiment this repository is built for varies what an agent is given while
holding the dataset bytes fixed. Modes are therefore part of the tool surface
from v0.1 -- not a retrofit -- even though only the first two are implemented
yet. Asking for an unimplemented mode fails loudly rather than silently
degrading to a weaker one, because a silent downgrade would corrupt a run.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ModeError


@dataclass(frozen=True)
class Mode:
    name: str
    available_since: str | None  # None => specified but not implemented yet
    tools: tuple[str, ...]
    description: str


_CORE_TOOLS = (
    "dataset_inventory",
    "list_files",
    "inspect_file",
    "inspect_table",
    "get_metadata",
    "get_evidence",
    "get_provenance",
)

# Each FAIR mode is a superset of the one before it: the ladder varies the form
# of the constraint, never the information underneath.
_FAIR_RULE_TOOLS = ("get_fair_indicator", "list_fair_rules")
_FAIR_CHECK_TOOLS = ("run_fair_check", "validate_identifier")

MODES: dict[str, Mode] = {
    "raw": Mode(
        name="raw",
        available_since="0.1.0",
        tools=("list_files", "inspect_file"),
        description="Files only. The control condition: no profiles, no evidence ledger.",
    ),
    "structured": Mode(
        name="structured",
        available_since="0.1.0",
        tools=(*_CORE_TOOLS, "resolve_identifier"),
        description="The full deterministic Data2MCP surface over an ingested dataset.",
    ),
    "fair-skill": Mode(
        name="fair-skill",
        available_since=None,
        tools=(*_CORE_TOOLS, "resolve_identifier"),
        description=(
            "structured + FAIR guidance as prose, generated from the same canonical "
            "rules. Planned for v0.3."
        ),
    ),
    "fair-rules": Mode(
        name="fair-rules",
        available_since="0.2.0",
        tools=(*_CORE_TOOLS, "resolve_identifier", *_FAIR_RULE_TOOLS),
        description=(
            "structured + the machine-readable FAIR rule registry. The agent can read "
            "every canonical rule, but must run the assessment itself."
        ),
    ),
    "fair-deterministic": Mode(
        name="fair-deterministic",
        available_since="0.2.0",
        tools=(*_CORE_TOOLS, "resolve_identifier", *_FAIR_RULE_TOOLS, *_FAIR_CHECK_TOOLS),
        description=(
            "fair-rules + deterministic implementations of the checks. The reference "
            "condition: verdicts come from code, not from the model."
        ),
    ),
    "fair-semantic": Mode(
        name="fair-semantic",
        available_since=None,
        tools=(
            *_CORE_TOOLS,
            "resolve_identifier",
            *_FAIR_RULE_TOOLS,
            *_FAIR_CHECK_TOOLS,
            "validate_vocabulary",
            "validate_shacl",
        ),
        description="fair-deterministic + vocabularies and SHACL. Planned for v0.4.",
    ),
}

DEFAULT_MODE = "structured"


def resolve_mode(name: str) -> Mode:
    """Return a mode, refusing anything unknown or not yet implemented."""
    mode = MODES.get(name)
    if mode is None:
        known = ", ".join(sorted(MODES))
        raise ModeError(f"unknown mode {name!r}; known modes are: {known}")
    if mode.available_since is None:
        raise ModeError(
            f"mode {name!r} is specified but not implemented in this version "
            f"({mode.description}). Refusing to substitute a different mode."
        )
    return mode
