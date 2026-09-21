"""Deterministic dataset ingestion -- no model, no inference, no interpretation."""

from .pipeline import IngestResult, ingest, write_json

__all__ = ["IngestResult", "ingest", "write_json"]
