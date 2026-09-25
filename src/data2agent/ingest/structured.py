"""JSON profiling.

We record a JSON document's *shape* -- its top-level type, its keys, its depth --
so an agent can decide whether to read it, without us paraphrasing it. Values are
never summarised or reinterpreted here; ``get_metadata`` serves them verbatim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .textio import read_text

_MAX_LISTED_KEYS = 200


@dataclass
class StructuredProfile:
    path: str
    root_type: str
    keys: list[str]
    item_count: int | None
    max_depth: int
    parse_error: str | None = None
    boris: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "root_type": self.root_type,
            "keys": list(self.keys),
            "max_depth": self.max_depth,
        }
        if self.item_count is not None:
            payload["item_count"] = self.item_count
        if self.parse_error is not None:
            payload["parse_error"] = self.parse_error
        # Only for files detected as BORIS projects, so every other structured
        # entry -- and every manifest without one -- is byte-for-byte unchanged.
        if self.boris is not None:
            payload["boris"] = dict(self.boris)
        return payload


def profile_json(path: Path, relative_path: str) -> StructuredProfile:
    """Profile a JSON file; a parse failure is reported, never swallowed."""
    return profile_document(*load_json(path), relative_path=relative_path)


def profile_document(
    document: Any | None, error: str | None, *, relative_path: str, boris: bool = False
) -> StructuredProfile:
    """Profile an already-loaded document.

    Split out from :func:`profile_json` so that a caller which needs the parsed
    document as well -- metadata recognition does -- can parse the file once.
    Parsing it twice would double the cost and, worse, admit the possibility of
    two different readings of the same bytes.
    """
    if error is not None:
        return StructuredProfile(relative_path, "invalid", [], None, 0, error)

    if isinstance(document, dict):
        keys = sorted(document.keys())[:_MAX_LISTED_KEYS]
        return StructuredProfile(
            relative_path,
            "object",
            keys,
            len(document),
            _depth(document),
            boris=boris_summary(document) if boris else None,
        )
    if isinstance(document, list):
        return StructuredProfile(relative_path, "array", [], len(document), _depth(document))
    return StructuredProfile(relative_path, type(document).__name__, [], None, 1)


def boris_summary(document: dict[str, Any]) -> dict[str, object]:
    """Count what a BORIS project holds, without repeating any of it.

    A BORIS project names its subjects, describes its behaviours and records
    each observation's media paths and free-text notes. None of that is
    recorded here: only how many observations, subjects and ethogram behaviours
    there are, plus the project format version, which says which BORIS layout
    to expect when the file is read. A section that is absent or not a
    collection is reported as ``None`` -- not zero, because "not there" and
    "there and empty" are different findings.
    """

    def count(key: str) -> int | None:
        section = document.get(key)
        return len(section) if isinstance(section, (dict, list)) else None

    version = document.get("project_format_version")
    # A version is a short token ("7.0"). Anything else -- a nested structure,
    # a long string -- is not recorded, rather than copied into the manifest.
    if isinstance(version, bool) or not isinstance(version, (str, int, float)):
        version = None
    elif isinstance(version, str) and len(version) > _MAX_VERSION_LENGTH:
        version = None
    return {
        "project_format_version": version,
        "observations": count("observations"),
        "subjects": count("subjects_conf"),
        "behaviors": count("behaviors_conf"),
    }


_MAX_VERSION_LENGTH = 32


def load_json(path: Path) -> tuple[Any | None, str | None]:
    """Load a JSON document verbatim, returning ``(document, error)``."""
    text, _ = read_text(path)
    if text is None:
        return None, "file could not be read as UTF-8 text"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as error:
        return None, str(error)


def _depth(node: Any, current: int = 1) -> int:
    if isinstance(node, dict):
        return max((_depth(value, current + 1) for value in node.values()), default=current)
    if isinstance(node, list):
        return max((_depth(item, current + 1) for item in node), default=current)
    return current
