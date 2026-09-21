"""Run provenance.

Provenance is kept in its own document on purpose. ``manifest.json`` must be
byte-identical across repeated ingests of the same bytes -- which it cannot be if
it carries a wall-clock timestamp -- so everything run-specific (when, where,
with what version, how long) lives here instead. The two documents are joined by
``dataset_id``.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import __version__


def utc_now() -> str:
    """An RFC 3339 timestamp in UTC, to whole seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class ProvenanceRecord:
    """What produced this ingest, and under what conditions."""

    dataset_id: str
    source_path: str
    output_path: str
    started_at: str
    finished_at: str
    tool: dict[str, str] = field(default_factory=dict)
    runtime: dict[str, str] = field(default_factory=dict)
    configuration: dict[str, Any] = field(default_factory=dict)
    source_mutated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "provenance_version": "0.1.0",
            "dataset_id": self.dataset_id,
            "activity": "data2agent.ingest",
            "source_path": self.source_path,
            "output_path": self.output_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "tool": self.tool,
            "runtime": self.runtime,
            "configuration": self.configuration,
            # Ingestion is read-only over the source tree. This flag exists so
            # that a future curation/export step has an honest place to say
            # otherwise, rather than quietly changing the meaning of a run.
            "source_mutated": self.source_mutated,
        }


def runtime_fingerprint() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.system(),
        "executable": sys.executable,
    }


def tool_fingerprint() -> dict[str, str]:
    return {"name": "data2agent", "version": __version__}
