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

_MAX_LISTED_KEYS = 200


@dataclass
class StructuredProfile:
    path: str
    root_type: str
    keys: list[str]
    item_count: int | None
    max_depth: int
    parse_error: str | None = None

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
        return payload


def profile_json(path: Path, relative_path: str) -> StructuredProfile:
    """Profile a JSON file; a parse failure is reported, never swallowed."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
        return StructuredProfile(relative_path, "invalid", [], None, 0, str(error))

    if isinstance(document, dict):
        keys = sorted(document.keys())[:_MAX_LISTED_KEYS]
        return StructuredProfile(relative_path, "object", keys, len(document), _depth(document))
    if isinstance(document, list):
        return StructuredProfile(relative_path, "array", [], len(document), _depth(document))
    return StructuredProfile(relative_path, type(document).__name__, [], None, 1)


def load_json(path: Path) -> tuple[Any | None, str | None]:
    """Load a JSON document verbatim, returning ``(document, error)``."""
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as error:
        return None, str(error)


def _depth(node: Any, current: int = 1) -> int:
    if isinstance(node, dict):
        return max((_depth(value, current + 1) for value in node.values()), default=current)
    if isinstance(node, list):
        return max((_depth(item, current + 1) for item in node), default=current)
    return current
