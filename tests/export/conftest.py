"""Synthetic fixtures for the annotation exporter. No real dataset bytes, no vendored schema.

A dataspace is built in ``tmp_path``: the folder layout the exporter relies on, a
small synthetic dataset under ``data/original/<family>``, and a *stand-in* annotation
schema that re-states only the constraints the exporter depends on.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from export_helpers import AGENT_MODEL, CREATED_AT, DATASET_PATH, FAMILY, STANDIN_SCHEMA

from data2agent.ingest import ingest


@pytest.fixture
def dataspace(tmp_path: Path) -> Path:
    """A synthetic dataspace holding one ingestable dataset, with the stand-in schema."""
    root = tmp_path / "dataspace"
    for sub in ("annotations/proposed", "annotations/curated", "schemas", "data/curated"):
        (root / sub).mkdir(parents=True)
    dataset = root / DATASET_PATH
    dataset.mkdir(parents=True)
    # Bytes, not text: write_text would turn \n into \r\n on Windows, so checksums
    # and the content-addressed claim ids would differ from those on Linux.
    (dataset / "t.csv").write_bytes(b"id,Trial,v\n1,Trial   1,2.5\n2,Trial   2,3.5\n")
    (dataset / "README.md").write_bytes(b"# Synthetic\n\nA synthetic table.\n")
    (root / "schemas" / "annotation.schema.json").write_text(
        json.dumps(STANDIN_SCHEMA), encoding="utf-8"
    )
    return root


@pytest.fixture
def ingest_dir(dataspace: Path, tmp_path: Path) -> Path:
    """Ingest output of the dataspace's dataset. Holds no assessment yet."""
    out = tmp_path / "ingest-out"
    ingest(dataspace / DATASET_PATH, out)
    return out


@pytest.fixture
def manifest(ingest_dir: Path) -> dict[str, Any]:
    return json.loads((ingest_dir / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture
def claim_for(ingest_dir: Path) -> Callable[..., str]:
    """Claim id of the first ledger claim whose subject is the given file.

    The ledger is ordered by content-addressed claim id, so "first" is arbitrary;
    pass ``check`` when the test depends on which evidence check the claim carries.
    """
    claims = json.loads((ingest_dir / "evidence.json").read_text(encoding="utf-8"))["claims"]

    def find(subject: str, check: str | None = None) -> str:
        return next(
            c["claim_id"]
            for c in claims
            if c["subject"] == subject
            and (check is None or any(e["check"] == check for e in c["evidence"]))
        )

    return find


@pytest.fixture
def write_assessment(ingest_dir: Path, manifest: dict[str, Any]) -> Callable[..., Path]:
    """Write ``assessment[.<profile>].json`` for the ingest output from hand-made results."""

    def write(profile_id: str, results: list[dict[str, Any]], *, version: str = "0.1.0") -> Path:
        name = "assessment.json" if profile_id == "fair" else f"assessment.{profile_id}.json"
        path = ingest_dir / name
        path.write_text(
            json.dumps(
                {
                    "assessment_version": "0.1.0",
                    "dataset_id": manifest["dataset_id"],
                    "profile": {"id": profile_id, "version": version},
                    "generator": {"mode": "fair-deterministic"},
                    "results": results,
                    "unsupported_claims": [],
                }
            ),
            encoding="utf-8",
        )
        return path

    return write


@pytest.fixture
def export(dataspace: Path, ingest_dir: Path) -> Callable[..., dict[str, Any]]:
    """``export_annotations`` with the fixture's paths and fixed model/time pre-filled."""
    from data2agent.export import export_annotations

    def run(**overrides: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "dataspace": dataspace,
            "dataset_path": DATASET_PATH,
            "family": FAMILY,
            "variant": "original",
            "agent_model": AGENT_MODEL,
            "created_at": CREATED_AT,
        }
        arguments.update(overrides)
        return export_annotations(ingest_dir, **arguments)

    return run
