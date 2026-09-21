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
)

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
        description="structured + FAIR guidance as prose. Planned for v0.2.",
    ),
    "fair-rules": Mode(
        name="fair-rules",
        available_since=None,
        tools=(*_CORE_TOOLS, "resolve_identifier", "get_fair_indicator"),
        description="structured + the machine-readable FAIR rule registry. Planned for v0.2.",
    ),
    "fair-deterministic": Mode(
        name="fair-deterministic",
        available_since=None,
        tools=(*_CORE_TOOLS, "resolve_identifier", "run_fair_check", "validate_identifier"),
        description="structured + deterministic FAIR check tools. Planned for v0.2.",
    ),
    "fair-semantic": Mode(
        name="fair-semantic",
        available_since=None,
        tools=(
            *_CORE_TOOLS,
            "resolve_identifier",
            "run_fair_check",
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
