"""The host-agnostic dataset service behind Data2MCP.

Every method that returns file content first re-checksums the file against the
manifest. If the bytes have moved, the service says so instead of answering: an
answer drawn from bytes that no longer match the dataset identity is worse than
no answer, because it looks exactly like a good one.

This module imports nothing from ``mcp`` and nothing host-specific, so it is
directly testable and directly reusable by any harness.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import query, relationships
from ..errors import ModeError, OutputError
from ..evidence import EvidenceLedger
from ..ingest.checksum import hash_file
from ..ingest.conventions import MissingValueConvention
from ..ingest.pipeline import EVIDENCE_FILENAME, MANIFEST_FILENAME, PROVENANCE_FILENAME
from ..readers.rows import read_delimited_rows, read_workbook_rows
from .modes import ALL_RESOURCES, DEFAULT_MODE, Mode, resolve_mode

# Content is served in bounded slices; an agent that wants more asks again.
_DEFAULT_PREVIEW_BYTES = 4096
_MAX_PREVIEW_BYTES = 262_144
_DEFAULT_READ_ROWS = 100
_MAX_READ_ROWS = 1000
_MAX_FILTER_SCAN_ROWS = 100_000
_MAX_COMPLETE_QUERY_ROWS = 100_000
_MAX_JOIN_SCAN_ROWS = 50_000


@dataclass
class IntegrityStatus:
    """Whether a file on disk still matches what the manifest recorded."""

    path: str
    expected_sha256: str
    observed_sha256: str | None
    matches: bool
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "expected_sha256": self.expected_sha256,
            "observed_sha256": self.observed_sha256,
            "matches": self.matches,
            **({"detail": self.detail} if self.detail else {}),
        }


class DatasetService:
    """Read-only access to one ingested dataset."""

    def __init__(
        self,
        output_dir: Path,
        *,
        source_dir: Path | None = None,
        mode: str = DEFAULT_MODE,
        load_relationships: bool = True,
    ) -> None:
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.mode: Mode = resolve_mode(mode)

        self.manifest = _load_json(self.output_dir / MANIFEST_FILENAME)
        # The manifest records how the bytes were read (missing-value convention,
        # profiles); relationship assessments are only valid under that reading.
        self.manifest_sha256 = hash_file(self.output_dir / MANIFEST_FILENAME)
        self.provenance = _load_json(self.output_dir / PROVENANCE_FILENAME)
        evidence = _load_json(self.output_dir / EVIDENCE_FILENAME)
        _require_one_dataset(self.manifest, self.provenance, evidence)
        self.ledger = EvidenceLedger.from_dict(evidence)
        self.relationship_bundle = (
            _load_relationship_bundle(
                self.output_dir / relationships.RELATIONSHIPS_FILENAME,
                self.dataset_id,
                self.manifest_sha256,
            )
            if load_relationships
            else None
        )

        recorded_source = self.provenance.get("source_path")
        candidate = (
            Path(source_dir) if source_dir else (Path(recorded_source) if recorded_source else None)
        )
        if candidate is None:
            raise OutputError(
                f"{PROVENANCE_FILENAME} does not record a source path; pass source_dir explicitly"
            )
        self.source_dir = candidate.expanduser().resolve()
        self._by_path = {entry["path"]: entry for entry in self.manifest.get("files", [])}
        self._profiles: dict[str, Any] = {}

    # -- capability surface -------------------------------------------------

    @property
    def dataset_id(self) -> str:
        return self.manifest.get("dataset_id", "")

    @property
    def ingested_at(self) -> str | None:
        """When the ingest that produced this output began reading the bytes."""
        return self.provenance.get("started_at")

    def available_tools(self) -> list[str]:
        return list(self.mode.tools)

    def available_resources(self) -> list[str]:
        return list(self.mode.resources)

    def supports(self, tool: str) -> bool:
        return tool in self.mode.tools

    def serves(self, uri: str) -> bool:
        """Whether this mode may serve a resource URI, templates included."""
        if uri in self.mode.resources:
            return True
        return (
            uri.startswith("dataset://files/") and "dataset://files/{path}" in self.mode.resources
        )

    # -- tools --------------------------------------------------------------

    def dataset_inventory(self) -> dict[str, Any]:
        """Summarise the dataset without reading any file content."""
        relationship_listing = self.list_relationships()
        return {
            "dataset_id": self.dataset_id,
            "manifest_version": self.manifest.get("manifest_version"),
            "root_name": self.manifest.get("source", {}).get("root_name"),
            # Run-specific, so it lives in provenance.json rather than the
            # manifest -- but surfaced here, because "when was this ingested?"
            # is the first thing anyone asks.
            "ingested_at": self.ingested_at,
            "ingest_duration_s": self.provenance.get("duration_seconds"),
            "tool_version": self.provenance.get("tool", {}).get("version"),
            "missing_value_convention": self.manifest.get("missing_value_convention", {}),
            "file_count": self.manifest.get("file_count", 0),
            "total_bytes": self.manifest.get("total_bytes", 0),
            "formats": self.manifest.get("formats", {}),
            "table_count": len(self.manifest.get("tables", {})),
            "metadata_files": self.manifest.get("metadata_files", []),
            "metadata_candidates": self.manifest.get("metadata_candidates", []),
            "identifier_count": len(self.manifest.get("identifiers", [])),
            # Relationship resolution is a derived layer, not part of ingest.
            # Without relationships.json, empty means "not determined".
            "relationships": relationship_listing["relationships"],
            "relationships_determined": relationship_listing["determined"],
            "relationship_status_counts": relationship_listing.get("status_counts", {}),
            "relationships_integrity": relationship_listing.get("source_integrity"),
            "warnings": self.manifest.get("warnings", []),
            "skipped": self.manifest.get("skipped", []),
            "mode": self.mode.name,
            "evidence_claims": len(self.ledger),
        }

    def list_files(
        self, *, pattern: str | None = None, file_format: str | None = None
    ) -> list[dict[str, Any]]:
        """List inventoried files, optionally filtered by glob and/or format."""
        results = []
        for entry in self.manifest.get("files", []):
            if pattern and not fnmatch.fnmatch(entry["path"], pattern):
                continue
            if file_format and entry.get("format") != file_format:
                continue
            results.append(entry)
        return results

    def inspect_file(
        self, path: str, *, preview_bytes: int = _DEFAULT_PREVIEW_BYTES
    ) -> dict[str, Any]:
        """Return a file's manifest record, integrity status, and a bounded preview."""
        entry = self._require_entry(path)
        absolute = self._resolve(path)
        integrity = self.verify_file(path)

        payload: dict[str, Any] = {
            "path": entry["path"],
            "size": entry["size"],
            "sha256": entry["sha256"],
            "format": entry.get("format"),
            "media_type": entry.get("media_type"),
            "detected_by": entry.get("detected_by"),
            "integrity": integrity.as_dict(),
        }
        # Present only when the name made a claim, mirroring the manifest. This
        # method promises the file's manifest record, so omitting a recorded
        # field would make the promise false.
        if "extension_format" in entry:
            payload["extension_format"] = entry["extension_format"]
            payload["extension_conflict"] = entry.get("extension_conflict", False)
        if not integrity.matches:
            payload["preview"] = None
            payload["preview_withheld"] = (
                "the file no longer matches its manifest checksum; re-ingest before relying on it"
            )
            return payload

        limit = max(0, min(int(preview_bytes), _MAX_PREVIEW_BYTES))
        # read(limit), not read_bytes()[:limit]: slicing after the fact would pull
        # a multi-gigabyte file entirely into memory to hand back 4 KiB of it, and
        # take the server down with it.
        with absolute.open("rb") as handle:
            raw = handle.read(limit)
        try:
            payload["preview"] = raw.decode("utf-8")
            payload["preview_encoding"] = "utf-8"
        except UnicodeDecodeError:
            payload["preview"] = None
            payload["preview_encoding"] = "binary"
            payload["preview_note"] = "file is not valid UTF-8; no textual preview is offered"
        payload["preview_bytes"] = len(raw)
        payload["preview_truncated"] = len(raw) < entry["size"]
        return payload

    def inspect_table(self, path: str) -> dict[str, Any]:
        """Return the recorded profile of a table, delimited or a worksheet.

        A worksheet is keyed ``<workbook path>#<sheet name>`` because one file
        yields many tables. That key is not an inventoried path, so integrity is
        verified against the workbook that backs it -- checking the key itself
        would fail, and returning the profile without checking anything would
        hand back content whose provenance was never confirmed.
        """
        tables = self.manifest.get("tables", {})
        profile = tables.get(path)
        backing = profile.get("workbook") if isinstance(profile, dict) else None
        if backing is None and "#" in path:
            backing = path.split("#", 1)[0]
        self._require_entry(backing or path)

        if profile is None:
            if "#" not in path and any(key.startswith(f"{path}#") for key in tables):
                sheets = sorted(k for k in tables if k.startswith(f"{path}#"))
                raise KeyError(
                    f"'{path}' is a workbook holding {len(sheets)} sheet(s); "
                    f"inspect one of {sheets}"
                )
            raise KeyError(
                f"'{path}' was not profiled as a table; "
                "call inspect_file for its format and preview"
            )
        return {**profile, "integrity": self.verify_file(backing or path).as_dict()}

    def list_tables(self) -> dict[str, Any]:
        """List profiled tables and worksheets without returning their observations."""
        tables: list[dict[str, Any]] = []
        for path, profile in sorted(self.manifest.get("tables", {}).items()):
            if not isinstance(profile, dict):
                continue
            backing = profile.get("workbook") or path
            tables.append(
                {
                    "path": path,
                    "kind": "worksheet" if profile.get("workbook") else "delimited",
                    "backing_file": backing,
                    "profiled": profile.get("profiled", True),
                    "rows": profile.get("rows"),
                    "columns": [column.get("name") for column in profile.get("columns", [])],
                    "warnings": list(profile.get("warnings", [])),
                }
            )
        return {"dataset_id": self.dataset_id, "tables": tables, "total": len(tables)}

    def read_rows(
        self,
        path: str,
        *,
        columns: list[str] | None = None,
        offset: int = 0,
        limit: int = _DEFAULT_READ_ROWS,
    ) -> dict[str, Any]:
        """Read actual observations from a profiled table through a bounded, read-only API.

        This is deliberately separate from :meth:`inspect_table`: the latter
        returns structural facts recorded at ingest, while this method returns
        source observations and therefore re-verifies the backing file checksum
        before reading anything.
        """
        tables = self.manifest.get("tables", {})
        profile = tables.get(path)
        if not isinstance(profile, dict):
            if "#" not in path and any(key.startswith(f"{path}#") for key in tables):
                sheets = sorted(key for key in tables if key.startswith(f"{path}#"))
                raise KeyError(
                    f"'{path}' is a workbook holding {len(sheets)} sheet(s); read one of {sheets}"
                )
            raise KeyError(f"'{path}' was not profiled as a table")

        if profile.get("profiled") is False:
            raise KeyError(f"'{path}' exists but was not successfully profiled")

        backing = profile.get("workbook") or path
        entry = self._require_entry(backing)
        integrity = self.verify_file(backing)

        available = [column["name"] for column in profile.get("columns", [])]
        selected = available if columns is None else list(columns)
        unknown = [name for name in selected if name not in available]
        if unknown:
            raise KeyError(
                f"unknown column(s) for '{path}': {unknown}; available columns: {available}"
            )

        requested_limit = int(limit)
        if requested_limit < 1:
            raise ValueError("limit must be at least 1")
        applied_limit = min(requested_limit, _MAX_READ_ROWS)
        applied_offset = int(offset)
        if applied_offset < 0:
            raise ValueError("offset must be zero or greater")

        payload: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "table": path,
            "backing_file": backing,
            "backing_sha256": entry["sha256"],
            "columns": selected,
            "offset": applied_offset,
            "limit_requested": requested_limit,
            "limit_applied": applied_limit,
            "integrity": integrity.as_dict(),
            "missing_value_convention": self.manifest.get("missing_value_convention", {}),
        }
        if not integrity.matches:
            payload.update(
                {
                    "rows": [],
                    "returned": 0,
                    "content_withheld": (
                        "the backing file no longer matches its manifest checksum; "
                        "re-ingest before relying on it"
                    ),
                }
            )
            return payload

        convention = _missing_convention(self.manifest.get("missing_value_convention", {}))
        absolute = self._resolve(backing)
        if profile.get("workbook"):
            observed = read_workbook_rows(
                absolute,
                profile,
                columns=selected,
                offset=applied_offset,
                limit=applied_limit,
                convention=convention,
            )
            payload["row_locator"] = "1-based worksheet row"
        else:
            observed = read_delimited_rows(
                absolute,
                profile,
                columns=selected,
                offset=applied_offset,
                limit=applied_limit,
                convention=convention,
            )
            payload["row_locator"] = (
                "1-based physical line on which the CSV/TSV logical record ends"
            )

        total_rows = profile.get("rows")
        payload.update(
            {
                "rows": observed,
                "returned": len(observed),
                "total_rows": total_rows,
                "has_more": (
                    isinstance(total_rows, int) and applied_offset + len(observed) < total_rows
                ),
            }
        )
        return payload

    def filter_rows(
        self,
        path: str,
        *,
        filters: list[dict[str, Any]],
        columns: list[str] | None = None,
        limit: int = _DEFAULT_READ_ROWS,
    ) -> dict[str, Any]:
        """Filter observations through a closed deterministic operator registry."""
        requested_limit = _bounded_result_limit(limit)
        profile = self._table_profile(path)
        available = [column["name"] for column in profile.get("columns", [])]
        output_columns = available if columns is None else list(columns)
        filter_columns = [
            rule.get("column") for rule in filters if isinstance(rule.get("column"), str)
        ]
        needed = _ordered_union(output_columns, filter_columns)
        _require_known_columns(path, available, needed)

        context, rows = self._scan_table(
            path, columns=needed, max_rows=_MAX_FILTER_SCAN_ROWS, require_complete=False
        )
        payload: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "operation": {
                "type": "filter_rows",
                "table": path,
                "filters": filters,
                "columns": output_columns,
                "limit": requested_limit,
            },
            "input": context,
            "scanned_rows": len(rows),
            "scan_complete": context["scan_complete"],
        }
        if not context["integrity"]["matches"]:
            payload.update(
                {
                    "rows": [],
                    "returned": 0,
                    "matches_in_scanned_rows": 0,
                    "content_withheld": context["content_withheld"],
                }
            )
            return payload

        matched, total_matches = query.filter_rows(rows, filters, limit=requested_limit)
        projected = [_project_row(row, output_columns) for row in matched]
        payload.update(
            {
                "rows": projected,
                "returned": len(projected),
                "matches_in_scanned_rows": total_matches,
                "truncated": total_matches > len(projected) or not context["scan_complete"],
            }
        )
        return payload

    def aggregate(
        self,
        path: str,
        *,
        group_by: list[str] | None = None,
        metrics: list[dict[str, Any]],
        filters: list[dict[str, Any]] | None = None,
        unit: list[str] | None = None,
        unit_metrics: list[dict[str, Any]] | None = None,
        on_inconsistent_unit: str = "refuse",
        unit_sample: int = 50,
    ) -> dict[str, Any]:
        """Compute deterministic group summaries from a complete bounded table scan.

        Without ``unit`` every row is an observation. With ``unit`` the rows are
        first reduced to one record per unit by ``unit_metrics`` and ``metrics``
        then summarise units -- see :mod:`data2agent.query.units`.
        """
        groups = list(group_by or [])
        rules = list(filters or [])
        unit_columns = list(unit or [])
        stage_one = list(unit_metrics or [])
        if stage_one and not unit_columns:
            raise ValueError("unit_metrics requires a unit declaration")
        profile = self._table_profile(path)
        available = [column["name"] for column in profile.get("columns", [])]
        dtypes = {column["name"]: column.get("dtype", "string") for column in profile["columns"]}
        # With a unit, the group-stage metrics read unit_metrics outputs, not
        # table columns, so only the unit stage decides which columns are read.
        row_metrics = stage_one if unit_columns else metrics
        metric_columns = [
            metric.get("column")
            for metric in row_metrics
            if isinstance(metric, dict) and isinstance(metric.get("column"), str)
        ]
        filter_columns = [
            rule.get("column") for rule in rules if isinstance(rule.get("column"), str)
        ]
        needed = _ordered_union(groups, unit_columns, metric_columns, filter_columns)
        _require_known_columns(path, available, needed)

        context, rows = self._scan_table(
            path,
            columns=needed,
            max_rows=_MAX_COMPLETE_QUERY_ROWS,
            require_complete=True,
        )
        payload: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "operation": {
                "type": "aggregate",
                "table": path,
                "group_by": groups,
                "metrics": metrics,
                "filters": rules,
                **(
                    {
                        "unit": unit_columns,
                        "unit_metrics": stage_one,
                        "on_inconsistent_unit": on_inconsistent_unit,
                    }
                    if unit_columns
                    else {}
                ),
            },
            "input": context,
            "scanned_rows": len(rows),
        }
        if not context["integrity"]["matches"]:
            payload.update({"groups": [], "content_withheld": context["content_withheld"]})
            return payload

        payload.update(
            _summarise(
                rows,
                dtypes=dtypes,
                group_by=groups,
                metrics=metrics,
                filters=rules,
                unit=unit_columns,
                unit_metrics=stage_one,
                on_inconsistent_unit=on_inconsistent_unit,
                unit_sample=unit_sample,
            )
        )
        return payload

    def aggregate_join(
        self,
        *,
        metrics: list[dict[str, Any]],
        relationship_id: str | None = None,
        left: str | None = None,
        right: str | None = None,
        left_keys: list[str] | None = None,
        right_keys: list[str] | None = None,
        how: str = "inner",
        group_by: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        unit: list[str] | None = None,
        unit_metrics: list[dict[str, Any]] | None = None,
        on_inconsistent_unit: str = "refuse",
        unit_sample: int = 50,
    ) -> dict[str, Any]:
        """Aggregate over the complete result of a join, optionally by unit of analysis.

        The grouping variable of an experiment often lives in a different table
        from the measurement (genotype in a registry, durations in a behaviour
        sheet). ``join_tables`` returns at most 1,000 joined rows, so summarising
        its output would summarise a page, not the data. This runs the same join
        to completion inside the bounds and aggregates that.

        Columns are addressed as ``left.<column>`` or ``right.<column>``: the
        prefix is split at the first dot only, so column names may themselves
        contain dots. An unqualified name is refused rather than resolved,
        because a name present on both sides is exactly the case where guessing
        is wrong.

        A many-to-many join is refused: it multiplies rows within each key, and
        a sum or mean over multiplied rows has no scientific reading.
        """
        groups = list(group_by or [])
        rules = list(filters or [])
        unit_columns = list(unit or [])
        stage_one = list(unit_metrics or [])
        if stage_one and not unit_columns:
            raise ValueError("unit_metrics requires a unit declaration")

        explicit = (left, right, left_keys, right_keys)
        contract: dict[str, Any] | None = None
        if relationship_id is not None:
            if any(value is not None for value in explicit):
                raise ValueError(
                    "give either relationship_id or left/right/left_keys/right_keys, not both"
                )
            record = self.get_relationship(relationship_id)["relationship"]
            if record["status"] not in {"declared", "deterministic"}:
                raise ValueError(
                    f"relationship {relationship_id!r} has status {record['status']!r}; "
                    "only declared or deterministic relationships can drive a named join"
                )
            left, right = record["left"]["table"], record["right"]["table"]
            left_keys = list(record["left"]["keys"])
            right_keys = list(record["right"]["keys"])
            contract = {
                "id": relationship_id,
                "status": record["status"],
                "cardinality": record["cardinality"],
                "basis": record["basis"],
            }
        elif any(value is None for value in explicit):
            raise ValueError(
                "aggregate_join needs relationship_id, or all of left, right, left_keys "
                "and right_keys"
            )
        assert left is not None and right is not None
        left_keys = [str(key) for key in left_keys or []]
        right_keys = [str(key) for key in right_keys or []]
        if how not in query.JOIN_TYPES:
            raise ValueError(
                f"unsupported join type {how!r}; choose from {sorted(query.JOIN_TYPES)}"
            )

        row_metrics = stage_one if unit_columns else metrics
        referenced = _ordered_union(
            groups,
            unit_columns,
            [
                metric.get("column")
                for metric in row_metrics
                if isinstance(metric, dict) and isinstance(metric.get("column"), str)
            ],
            [rule.get("column") for rule in rules if isinstance(rule.get("column"), str)],
        )
        sides: dict[str, list[str]] = {"left": [], "right": []}
        for name in referenced:
            side, column = _split_qualified(name)
            sides[side].append(column)

        left_profile = self._table_profile(left)
        right_profile = self._table_profile(right)
        left_needed = _ordered_union(left_keys, sides["left"])
        right_needed = _ordered_union(right_keys, sides["right"])
        _require_known_columns(
            left, [column["name"] for column in left_profile.get("columns", [])], left_needed
        )
        _require_known_columns(
            right, [column["name"] for column in right_profile.get("columns", [])], right_needed
        )

        left_context, left_rows = self._scan_table(
            left, columns=left_needed, max_rows=_MAX_JOIN_SCAN_ROWS, require_complete=True
        )
        right_context, right_rows = self._scan_table(
            right, columns=right_needed, max_rows=_MAX_JOIN_SCAN_ROWS, require_complete=True
        )
        join_spec = {
            "left": left,
            "right": right,
            "left_keys": left_keys,
            "right_keys": right_keys,
            "how": how,
        }
        payload: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "operation": {
                "type": "aggregate_join",
                "join": join_spec,
                "group_by": groups,
                "metrics": metrics,
                "filters": rules,
                **(
                    {
                        "unit": unit_columns,
                        "unit_metrics": stage_one,
                        "on_inconsistent_unit": on_inconsistent_unit,
                    }
                    if unit_columns
                    else {}
                ),
            },
            "inputs": {"left": left_context, "right": right_context},
        }
        if contract is not None:
            payload["relationship_contract"] = contract
        drifted = [
            side
            for side, context in (("left", left_context), ("right", right_context))
            if not context["integrity"]["matches"]
        ]
        if drifted:
            withheld = f"source drift detected on: {', '.join(drifted)}"
            payload.update({"groups": [], "content_withheld": withheld})
            return payload

        joined = query.join_rows(
            left_rows,
            right_rows,
            left_keys=left_keys,
            right_keys=right_keys,
            how=how,
            limit=_MAX_COMPLETE_QUERY_ROWS,
        )
        cardinality = joined["diagnostics"]["cardinality"]
        if cardinality == "many_to_many":
            raise ValueError(
                "refusing to aggregate a many-to-many join: both sides repeat join keys, so "
                "rows are multiplied within each key and every sum, mean and count over them "
                "is inflated. Declare keys that are unique on at least one side."
            )
        if joined["truncated"]:
            raise ValueError(
                f"the join produces {joined['total_result_rows']} rows, above the "
                f"complete-aggregation safety cap of {_MAX_COMPLETE_QUERY_ROWS}"
            )

        rows = [_flatten_joined_row(row, left_needed, right_needed) for row in joined["rows"]]
        dtypes = {
            **_qualified_dtypes("left", left_profile, left_needed),
            **_qualified_dtypes("right", right_profile, right_needed),
        }
        warnings = list(joined["warnings"])
        # In a one-to-many join the unique side's row is copied onto every match.
        # A row-level metric over one of its columns then counts that value once
        # per match -- an animal's body weight weighted by its number of bins.
        replicated = {"one_to_many": "left", "many_to_one": "right"}.get(cardinality)
        if replicated:
            touched = sorted(
                {
                    name
                    for name in [
                        metric.get("column")
                        for metric in row_metrics
                        if isinstance(metric, dict) and isinstance(metric.get("column"), str)
                    ]
                    if name.startswith(f"{replicated}.")
                }
            )
            if touched:
                warnings.append(
                    f"{cardinality} join: each {replicated} row is repeated once per match, so "
                    f"row-level metrics over {touched} weight each {replicated} value by its "
                    "number of matches"
                )
        payload["join"] = {
            "cardinality": cardinality,
            "diagnostics": joined["diagnostics"],
            "joined_rows": joined["total_result_rows"],
            "left_rows_without_match": sum(row["source_row"]["right"] is None for row in rows),
            "row_locator": {
                "left": left_context.get("row_locator"),
                "right": right_context.get("row_locator"),
            },
            "warnings": warnings,
        }
        payload.update(
            _summarise(
                rows,
                dtypes=dtypes,
                group_by=groups,
                metrics=metrics,
                filters=rules,
                unit=unit_columns,
                unit_metrics=stage_one,
                on_inconsistent_unit=on_inconsistent_unit,
                unit_sample=unit_sample,
            )
        )
        return payload

    def describe_variable(self, path: str, column: str) -> dict[str, Any]:
        """Describe one observed column without assigning scientific meaning to it."""
        profile = self._table_profile(path)
        columns = {item["name"]: item for item in profile.get("columns", [])}
        if column not in columns:
            raise KeyError(
                f"unknown column {column!r} for '{path}'; available columns: {list(columns)}"
            )

        context, rows = self._scan_table(
            path,
            columns=[column],
            max_rows=_MAX_COMPLETE_QUERY_ROWS,
            require_complete=True,
        )
        payload: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "operation": {"type": "describe_variable", "table": path, "column": column},
            "input": context,
            "profile": columns[column],
        }
        if not context["integrity"]["matches"]:
            payload.update({"summary": None, "content_withheld": context["content_withheld"]})
            return payload

        payload["summary"] = query.describe_column(
            rows, column=column, dtype=str(columns[column].get("dtype") or "string")
        )
        return payload

    def join_tables(
        self,
        left: str,
        right: str,
        *,
        left_keys: list[str],
        right_keys: list[str],
        left_columns: list[str] | None = None,
        right_columns: list[str] | None = None,
        how: str = "inner",
        limit: int = _DEFAULT_READ_ROWS,
    ) -> dict[str, Any]:
        """Join two tables on caller-declared keys; no relationship is inferred."""
        requested_limit = _bounded_result_limit(limit)
        left_profile = self._table_profile(left)
        right_profile = self._table_profile(right)
        left_available = [column["name"] for column in left_profile.get("columns", [])]
        right_available = [column["name"] for column in right_profile.get("columns", [])]
        left_output = left_available if left_columns is None else list(left_columns)
        right_output = right_available if right_columns is None else list(right_columns)
        left_needed = _ordered_union(left_output, left_keys)
        right_needed = _ordered_union(right_output, right_keys)
        _require_known_columns(left, left_available, left_needed)
        _require_known_columns(right, right_available, right_needed)

        left_context, left_rows = self._scan_table(
            left, columns=left_needed, max_rows=_MAX_JOIN_SCAN_ROWS, require_complete=True
        )
        right_context, right_rows = self._scan_table(
            right, columns=right_needed, max_rows=_MAX_JOIN_SCAN_ROWS, require_complete=True
        )
        payload: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "operation": {
                "type": "join_tables",
                "left": left,
                "right": right,
                "left_keys": left_keys,
                "right_keys": right_keys,
                "left_columns": left_output,
                "right_columns": right_output,
                "how": how,
                "limit": requested_limit,
            },
            "inputs": {"left": left_context, "right": right_context},
        }
        drifted = [
            side
            for side, context in (("left", left_context), ("right", right_context))
            if not context["integrity"]["matches"]
        ]
        if drifted:
            payload.update(
                {
                    "rows": [],
                    "returned": 0,
                    "content_withheld": f"source drift detected on: {', '.join(drifted)}",
                }
            )
            return payload

        result = query.join_rows(
            left_rows,
            right_rows,
            left_keys=left_keys,
            right_keys=right_keys,
            how=how,
            limit=requested_limit,
        )
        result["rows"] = [
            _project_joined_row(row, left_output, right_output) for row in result["rows"]
        ]
        payload.update(result)
        return payload

    def build_relationships(
        self,
        declarations: list[dict[str, Any]] | None = None,
        *,
        declaration_source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve structural candidates and explicit relationship declarations.

        Structural overlap is always a candidate. Only an explicit declaration
        (or a future supported deterministic convention) may produce a
        relationship eligible for named execution.
        """
        tables = {
            path: profile
            for path, profile in self.manifest.get("tables", {}).items()
            if isinstance(profile, dict) and profile.get("profiled", True)
        }
        supplied = list(declarations or [])
        records: dict[str, dict[str, Any]] = {}
        skipped: list[dict[str, Any]] = []

        for index, declaration in enumerate(supplied):
            spec = _validate_declaration(declaration, index)
            basis: dict[str, Any] = {
                "method": "explicit-declaration",
                "note": spec.get("note") or "relationship explicitly declared by configuration",
            }
            if declaration_source:
                basis["declaration_source"] = dict(declaration_source)
            record = self._assess_relationship_spec(
                spec,
                status="declared",
                basis=basis,
                expected_cardinality=spec.get("expected_cardinality"),
            )
            records[record["id"]] = record

        for spec in relationships.candidate_key_specs(tables):
            candidate_id = relationships.stable_relationship_id(
                self.dataset_id,
                spec["left"],
                spec["right"],
                spec["left_keys"],
                spec["right_keys"],
            )
            if candidate_id in records:
                continue
            try:
                record = self._assess_relationship_spec(
                    spec,
                    status="candidate",
                    basis=spec["basis"],
                )
            except ValueError as error:
                skipped.append(
                    {
                        "left": spec["left"],
                        "right": spec["right"],
                        "left_keys": spec["left_keys"],
                        "right_keys": spec["right_keys"],
                        "reason": str(error),
                    }
                )
                continue
            # A shared column name with zero observed key overlap is not a
            # relationship candidate; preserving it would turn absence into noise.
            if record["matched_distinct_keys"]:
                records[record["id"]] = record

        ordered = [records[key] for key in sorted(records)]
        status_counts: dict[str, int] = {}
        for record in ordered:
            status = record["status"]
            status_counts[status] = status_counts.get(status, 0) + 1

        payload: dict[str, Any] = {
            "relationships_version": relationships.RELATIONSHIP_VERSION,
            "dataset_id": self.dataset_id,
            "manifest_sha256": self.manifest_sha256,
            "determined": True,
            "relationship_count": len(ordered),
            "status_counts": dict(sorted(status_counts.items())),
            "relationships": ordered,
            "skipped": sorted(
                skipped,
                key=lambda item: (
                    item["left"],
                    item["right"],
                    tuple(item["left_keys"]),
                    tuple(item["right_keys"]),
                ),
            ),
        }
        if declaration_source:
            payload["declaration_source"] = dict(declaration_source)
        return payload

    def list_relationships(self, status: str | None = None) -> dict[str, Any]:
        """List saved relationships; absent sidecar means not determined, never none."""
        if self.relationship_bundle is None:
            return {
                "dataset_id": self.dataset_id,
                "determined": False,
                "relationships": [],
                "total": 0,
                "note": (
                    "relationship resolution has not been run; use "
                    "'data2agent relationships <output>' to create relationships.json"
                ),
            }
        records = list(self.relationship_bundle.get("relationships", []))
        backing_files = sorted(
            {
                endpoint["backing_file"]
                for record in records
                for endpoint in (record["left"], record["right"])
            }
        )
        integrity = [self.verify_file(path).as_dict() for path in backing_files]
        drifted = [item for item in integrity if not item["matches"]]
        if drifted:
            return {
                "dataset_id": self.dataset_id,
                "determined": bool(self.relationship_bundle.get("determined")),
                "relationships_version": self.relationship_bundle.get("relationships_version"),
                "relationships": [],
                "total": 0,
                "status_counts": self.relationship_bundle.get("status_counts", {}),
                "skipped": self.relationship_bundle.get("skipped", []),
                "source_integrity": {"matches": False, "files": integrity},
                "content_withheld": (
                    "saved relationship assertions were derived from source bytes that no "
                    "longer match the dataset manifest; re-ingest and regenerate relationships"
                ),
            }
        if status is not None:
            if status not in relationships.STATUSES:
                raise ValueError(
                    f"unknown relationship status {status!r}; "
                    f"choose from {sorted(relationships.STATUSES)}"
                )
            records = [record for record in records if record.get("status") == status]
        return {
            "dataset_id": self.dataset_id,
            "determined": bool(self.relationship_bundle.get("determined")),
            "relationships_version": self.relationship_bundle.get("relationships_version"),
            "relationships": records,
            "total": len(records),
            "status_counts": self.relationship_bundle.get("status_counts", {}),
            "skipped": self.relationship_bundle.get("skipped", []),
            "source_integrity": {"matches": True, "files": integrity},
        }

    def get_relationship(self, relationship_id: str) -> dict[str, Any]:
        """Return one saved relationship record by stable id."""
        listing = self.list_relationships()
        if not listing["determined"]:
            raise KeyError("relationships have not been determined for this dataset")
        if listing.get("content_withheld"):
            raise OutputError(listing["content_withheld"])
        for record in listing["relationships"]:
            if record.get("id") == relationship_id:
                return {"dataset_id": self.dataset_id, "relationship": record}
        raise KeyError(f"no relationship with id {relationship_id!r}")

    def join_relationship(
        self,
        relationship_id: str,
        *,
        left_columns: list[str] | None = None,
        right_columns: list[str] | None = None,
        how: str = "inner",
        limit: int = _DEFAULT_READ_ROWS,
    ) -> dict[str, Any]:
        """Execute a saved declared/deterministic relationship as a join contract."""
        record = self.get_relationship(relationship_id)["relationship"]
        status = record["status"]
        if status not in {"declared", "deterministic"}:
            raise ValueError(
                f"relationship {relationship_id!r} has status {status!r}; "
                "only declared or deterministic relationships can drive a named join"
            )
        result = self.join_tables(
            record["left"]["table"],
            record["right"]["table"],
            left_keys=list(record["left"]["keys"]),
            right_keys=list(record["right"]["keys"]),
            left_columns=left_columns,
            right_columns=right_columns,
            how=how,
            limit=limit,
        )
        result["relationship_contract"] = {
            "id": relationship_id,
            "status": status,
            "cardinality": record["cardinality"],
            "basis": record["basis"],
        }
        return result

    def get_metadata(self, path: str | None = None) -> dict[str, Any]:
        """Serve recognised metadata files verbatim.

        Verbatim is the point: paraphrasing a metadata record here would make the
        downstream claim unverifiable against the bytes.
        """
        recognised = {item["path"]: item for item in self.manifest.get("metadata_files", [])}
        candidates = self.manifest.get("metadata_candidates", [])
        if path is None:
            return {
                "dataset_id": self.dataset_id,
                "metadata_files": list(recognised.values()),
                # Reported beside the recognitions, never merged into them: a
                # candidate is a file a recogniser could not finish reading, so
                # an empty metadata_files next to a populated list here means
                # "nothing found, and these were never examined".
                "metadata_candidates": list(candidates),
                "note": "call get_metadata(path=...) for a file's verbatim contents",
            }
        if path not in recognised:
            known = ", ".join(sorted(recognised)) or "none recognised"
            raise KeyError(f"'{path}' is not a recognised metadata file; recognised: {known}")

        entry = recognised[path]
        carrier = entry.get("file") or path
        integrity = self.verify_file(carrier)
        payload: dict[str, Any] = {**entry, "integrity": integrity.as_dict()}
        if not integrity.matches:
            payload["content"] = None
            payload["content_withheld"] = "file no longer matches its manifest checksum"
            return payload

        if carrier != path:
            # Metadata recognised inside part of a file -- one worksheet of a
            # workbook. Serving the whole file's bytes would answer a different
            # question from the one asked, so the structure is reported and the
            # content is not invented.
            payload["content"] = None
            payload["content_encoding"] = "not-applicable"
            payload["content_withheld"] = (
                f"this metadata is carried inside '{carrier}' rather than being a file of its "
                f"own; its observed structure is in 'basis', and the profiled content is "
                f"available through inspect_table('{path}')"
            )
            return payload

        absolute = self._resolve(path)
        try:
            payload["content"] = absolute.read_text(encoding="utf-8")
            payload["content_encoding"] = "utf-8"
        except UnicodeDecodeError:
            payload["content"] = None
            payload["content_encoding"] = "binary"
        return payload

    def get_provenance(self) -> dict[str, Any]:
        """Return when, where and with what this dataset was ingested.

        Separate from the manifest by design, and separate from
        ``dataset_inventory`` because provenance answers a different question:
        not what the dataset is, but what this run of the tool was.
        """
        return {
            "dataset_id": self.dataset_id,
            "ingested_at": self.ingested_at,
            "started_at": self.provenance.get("started_at"),
            "finished_at": self.provenance.get("finished_at"),
            "duration_seconds": self.provenance.get("duration_seconds"),
            "tool": self.provenance.get("tool", {}),
            "runtime": self.provenance.get("runtime", {}),
            "configuration": self.provenance.get("configuration", {}),
            "source_verified_unchanged": self.provenance.get("source_verified_unchanged"),
            "source_mutated": self.provenance.get("source_mutated"),
            "note": (
                "timestamps describe this ingest run, not the dataset; manifest.json "
                "is timestamp-free so repeated ingests of the same bytes compare equal"
            ),
        }

    def get_evidence(
        self,
        *,
        claim_id: str | None = None,
        subject: str | None = None,
        check: str | None = None,
        contains: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Query the claim -> evidence ledger."""
        if claim_id:
            record = self.ledger.get(claim_id)
            if record is None:
                raise KeyError(f"no claim with id '{claim_id}'")
            return {"dataset_id": self.dataset_id, "claims": [record.as_dict()], "total": 1}

        matches = self.ledger.query(subject=subject, check=check, contains=contains)
        capped = matches[: max(1, int(limit))]
        return {
            "dataset_id": self.dataset_id,
            "claims": [record.as_dict() for record in capped],
            "total": len(matches),
            "returned": len(capped),
        }

    def resolve_identifier(self, value: str) -> dict[str, Any]:
        """Report where an identifier occurs in the dataset.

        This is occurrence lookup, not resolution: no network call is made, and a
        hit is never evidence that the identifier resolves or is valid. Actual
        resolution needs the network, which no check in this version makes;
        `validate_identifier` in a fair-* mode checks syntax only.
        """
        needle = value.strip().lower()
        occurrences = [
            hit for hit in self.manifest.get("identifiers", []) if hit["value"].lower() == needle
        ]
        return {
            "query": value,
            "occurrences": occurrences,
            "found": bool(occurrences),
            "resolved": None,
            "note": "occurrence lookup only; no resolution was attempted",
        }

    # -- profile tools (fair-* modes only) ----------------------------------

    def profile_context(self):
        """Build the read-only view a profile check is allowed to see.

        Deliberately narrow: the manifest, the evidence ledger, and the text of
        recognised metadata files. A check that could re-read the dataset could
        reach a conclusion the evidence ledger cannot account for.
        """
        from ..profiles.model import ProfileContext

        return ProfileContext(
            manifest=self.manifest,
            ledger=self.ledger,
            read_metadata=self._metadata_text,
        )

    def load_profile(self, profile_id: str = "fair"):
        """Load and cache an assessment profile."""
        from ..profiles.loader import load_profile

        if profile_id not in self._profiles:
            self._profiles[profile_id] = load_profile(profile_id)
        return self._profiles[profile_id]

    def list_fair_rules(self) -> dict[str, Any]:
        """List the canonical FAIR rules: id, principle, question, implementation status."""
        profile = self.load_profile("fair")
        return {
            "profile": {"id": profile.id, "version": profile.version, "title": profile.title},
            "description": profile.description,
            "rules": [
                {
                    "id": rule.id,
                    "principle": rule.principle,
                    "question": rule.question,
                    "check": rule.check_type,
                    "implemented": rule.implemented,
                    "allowed_results": list(rule.allowed_results),
                }
                for rule in profile.rules
            ],
        }

    def get_fair_indicator(self, rule_id: str) -> dict[str, Any]:
        """Return one canonical FAIR rule in full, exactly as authored."""
        return self.load_profile("fair").rule(rule_id).as_dict()

    def run_fair_check(
        self,
        rule_id: str | None = None,
        *,
        host: str | None = None,
        model: str | None = None,
        orchestrator: str | None = None,
    ) -> dict[str, Any]:
        """Run the deterministic FAIR checks and return an assessment.

        The verdicts come from code, not from a model. ``unknown`` results are
        preserved rather than resolved: a rule this version cannot run reports
        unknown and stays in the denominator.
        """
        from ..profiles.fair import CHECKS
        from ..profiles.runner import run

        profile = self.load_profile("fair")
        generator: dict[str, Any] = {
            "mode": self.mode.name,
            "orchestrator": orchestrator or "deterministic",
        }
        if host:
            generator["host"] = host
        if model:
            generator["model"] = model

        assessment = run(
            profile, self.profile_context(), CHECKS, generator=generator, rule_id=rule_id
        )
        payload = assessment.as_dict()
        payload["summary"] = assessment.summary()
        return payload

    def validate_identifier(self, value: str) -> dict[str, Any]:
        """Check an identifier's syntax against its scheme. No network request is made.

        Syntactic validity is not resolution. A well-formed DOI that points at
        nothing still passes here, and the response says so explicitly so the
        distinction cannot be lost downstream.
        """
        from ..ingest.identifiers import validate

        result = validate(value)
        result["resolution_attempted"] = False
        result["resolves"] = None
        result["note"] = (
            "syntax only; whether this identifier resolves was not checked and must not "
            "be inferred from a valid syntax"
        )
        occurrences = self.resolve_identifier(value)
        result["occurrences"] = occurrences["occurrences"]
        return result

    # -- resources ----------------------------------------------------------

    def resource(self, uri: str) -> str:
        """Serve a ``dataset://`` resource as text, subject to the mode's gating.

        Gating lives here rather than only in the MCP binding so that it holds
        for every caller -- a harness driving the service directly is bound by
        the same condition as one going through the protocol.
        """
        # A URI outside the registry does not exist; one inside it may still be
        # withheld by the mode. The two are different answers and different errors.
        if uri not in ALL_RESOURCES and not uri.startswith("dataset://files/"):
            raise KeyError(f"unknown resource uri: {uri}")
        if not self.serves(uri):
            allowed = ", ".join(self.mode.resources) or "none"
            raise ModeError(f"mode '{self.mode.name}' does not serve {uri!r}; it serves: {allowed}")
        if uri == "dataset://manifest":
            return json.dumps(self.manifest, indent=2, ensure_ascii=False)
        if uri == "dataset://provenance":
            return json.dumps(self.provenance, indent=2, ensure_ascii=False)
        if uri == "dataset://evidence":
            return json.dumps(self.ledger.as_dict(), indent=2, ensure_ascii=False)
        if uri == "dataset://metadata":
            return json.dumps(self.get_metadata(), indent=2, ensure_ascii=False)
        if uri == "dataset://relationships":
            return json.dumps(self.list_relationships(), indent=2, ensure_ascii=False)
        if uri.startswith("dataset://files/"):
            return json.dumps(
                self.inspect_file(uri[len("dataset://files/") :]), indent=2, ensure_ascii=False
            )
        raise KeyError(f"unknown resource uri: {uri}")

    # -- integrity ----------------------------------------------------------

    def verify_file(self, path: str) -> IntegrityStatus:
        entry = self._require_entry(path)
        absolute = self._resolve(path)
        if not absolute.is_file():
            return IntegrityStatus(
                path, entry["sha256"], None, False, "file is missing from the source tree"
            )
        observed = hash_file(absolute)
        return IntegrityStatus(path, entry["sha256"], observed, observed == entry["sha256"])

    def verify_dataset(self) -> dict[str, Any]:
        """Re-checksum every file and report drift against the manifest."""
        statuses = [self.verify_file(entry["path"]) for entry in self.manifest.get("files", [])]
        mismatched = [status for status in statuses if not status.matches]
        return {
            "dataset_id": self.dataset_id,
            "files_checked": len(statuses),
            "intact": not mismatched,
            "mismatched": [status.as_dict() for status in mismatched],
        }

    # -- internals ----------------------------------------------------------

    def _table_profile(self, path: str) -> dict[str, Any]:
        """Return one profiled table, preserving workbook-vs-sheet diagnostics."""
        tables = self.manifest.get("tables", {})
        profile = tables.get(path)
        if not isinstance(profile, dict):
            if "#" not in path and any(key.startswith(f"{path}#") for key in tables):
                sheets = sorted(key for key in tables if key.startswith(f"{path}#"))
                raise KeyError(
                    f"'{path}' is a workbook holding {len(sheets)} sheet(s); use one of {sheets}"
                )
            raise KeyError(f"'{path}' was not profiled as a table")
        if profile.get("profiled") is False:
            raise KeyError(f"'{path}' exists but was not successfully profiled")
        return profile

    def _scan_table(
        self,
        path: str,
        *,
        columns: list[str],
        max_rows: int,
        require_complete: bool,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Read a bounded query scan after exactly one backing-file integrity check."""
        profile = self._table_profile(path)
        available = [column["name"] for column in profile.get("columns", [])]
        _require_known_columns(path, available, columns)

        backing = profile.get("workbook") or path
        entry = self._require_entry(backing)
        integrity = self.verify_file(backing)
        total_rows = profile.get("rows")
        if require_complete and isinstance(total_rows, int) and total_rows > max_rows:
            raise ValueError(
                f"query requires a complete scan of '{path}', but it has {total_rows} rows "
                f"and the safety cap is {max_rows}"
            )

        context: dict[str, Any] = {
            "table": path,
            "backing_file": backing,
            "backing_sha256": entry["sha256"],
            "integrity": integrity.as_dict(),
            "total_rows": total_rows,
            "scan_limit": max_rows,
            "scan_complete": isinstance(total_rows, int) and total_rows <= max_rows,
        }
        if not integrity.matches:
            context["content_withheld"] = (
                "the backing file no longer matches its manifest checksum; "
                "re-ingest before relying on it"
            )
            return context, []

        convention = _missing_convention(self.manifest.get("missing_value_convention", {}))
        absolute = self._resolve(backing)
        if profile.get("workbook"):
            rows = read_workbook_rows(
                absolute,
                profile,
                columns=columns,
                offset=0,
                limit=max_rows,
                convention=convention,
            )
            context["row_locator"] = "1-based worksheet row"
        else:
            rows = read_delimited_rows(
                absolute,
                profile,
                columns=columns,
                offset=0,
                limit=max_rows,
                convention=convention,
            )
            context["row_locator"] = (
                "1-based physical line on which the CSV/TSV logical record ends"
            )

        if not isinstance(total_rows, int):
            context["scan_complete"] = len(rows) < max_rows
        return context, rows

    def _assess_relationship_spec(
        self,
        spec: dict[str, Any],
        *,
        status: str,
        basis: dict[str, Any],
        expected_cardinality: str | None = None,
    ) -> dict[str, Any]:
        left = str(spec["left"])
        right = str(spec["right"])
        left_keys = [str(value) for value in spec["left_keys"]]
        right_keys = [str(value) for value in spec["right_keys"]]

        left_context, left_rows = self._scan_table(
            left,
            columns=left_keys,
            max_rows=_MAX_JOIN_SCAN_ROWS,
            require_complete=True,
        )
        right_context, right_rows = self._scan_table(
            right,
            columns=right_keys,
            max_rows=_MAX_JOIN_SCAN_ROWS,
            require_complete=True,
        )
        drifted = [
            side
            for side, context in (("left", left_context), ("right", right_context))
            if not context["integrity"]["matches"]
        ]
        if drifted:
            raise OutputError(
                "cannot assess relationships against drifted source bytes: " + ", ".join(drifted)
            )
        return relationships.assess_relationship(
            dataset_id=self.dataset_id,
            left_table=left,
            right_table=right,
            left_keys=left_keys,
            right_keys=right_keys,
            left_rows=left_rows,
            right_rows=right_rows,
            left_context=left_context,
            right_context=right_context,
            status=status,
            basis=basis,
            expected_cardinality=expected_cardinality,
        )

    def _require_entry(self, path: str) -> dict[str, Any]:
        entry = self._by_path.get(path)
        if entry is None:
            raise KeyError(f"'{path}' is not in the dataset manifest")
        return entry

    def _resolve(self, path: str) -> Path:
        """Resolve a dataset-relative path, refusing to escape the dataset root."""
        absolute = (self.source_dir / path).resolve()
        if absolute != self.source_dir and self.source_dir not in absolute.parents:
            raise OutputError(f"path escapes the dataset root: {path}")
        return absolute

    def _metadata_text(self, path: str) -> str | None:
        """Read a recognised metadata file as text, or return None."""
        try:
            served = self.get_metadata(path)
        except KeyError:
            return None
        return served.get("content")


def _require_one_dataset(
    manifest: dict[str, Any], provenance: dict[str, Any], evidence: dict[str, Any]
) -> None:
    """Refuse an output directory whose three documents describe different datasets.

    They can disagree if an ingest was interrupted between writes, if a directory
    was partly overwritten by a second run, or if files from two runs were mixed.
    Serving that state would attach one dataset's evidence to another's manifest
    and label it with the manifest's id -- a wrong answer wearing the exact shape
    of a right one, which is the failure this whole design exists to prevent.
    """
    ids = {
        MANIFEST_FILENAME: manifest.get("dataset_id"),
        PROVENANCE_FILENAME: provenance.get("dataset_id"),
        EVIDENCE_FILENAME: evidence.get("dataset_id"),
    }
    if len(set(ids.values())) > 1:
        detail = ", ".join(f"{name}={value!r}" for name, value in sorted(ids.items()))
        raise OutputError(
            "this output directory does not describe a single dataset: "
            f"{detail}. Re-run `data2agent ingest` into a clean directory."
        )


def _validate_declaration(declaration: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(declaration, dict):
        raise ValueError(f"declaration {index} must be an object")
    required = ("left", "right", "left_keys", "right_keys")
    missing = [field for field in required if field not in declaration]
    if missing:
        raise ValueError(f"declaration {index} is missing required field(s): {missing}")

    left_keys = declaration["left_keys"]
    right_keys = declaration["right_keys"]
    if not isinstance(left_keys, list) or not isinstance(right_keys, list):
        raise ValueError(f"declaration {index} keys must be arrays")
    if not left_keys or len(left_keys) != len(right_keys):
        raise ValueError(
            f"declaration {index} left_keys/right_keys must be non-empty and equal length"
        )

    result = {
        "left": str(declaration["left"]),
        "right": str(declaration["right"]),
        "left_keys": [str(value) for value in left_keys],
        "right_keys": [str(value) for value in right_keys],
    }
    if declaration.get("expected_cardinality") is not None:
        expected = str(declaration["expected_cardinality"])
        if expected not in relationships.CARDINALITIES:
            raise ValueError(
                f"declaration {index} has unsupported expected_cardinality {expected!r}"
            )
        result["expected_cardinality"] = expected
    if declaration.get("note") is not None:
        result["note"] = str(declaration["note"])
    return result


def _load_relationship_bundle(
    path: Path, dataset_id: str, manifest_sha256: str
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("dataset_id") != dataset_id:
        raise OutputError(
            f"{path.name} belongs to dataset {payload.get('dataset_id')!r}, "
            f"but the manifest describes {dataset_id!r}; regenerate relationships"
        )
    # Same bytes, different reading: a re-ingest under another missing-value
    # convention keeps dataset_id but changes which keys exist, so saved overlap,
    # cardinality, and declared status no longer describe what a join would do.
    if payload.get("manifest_sha256") != manifest_sha256:
        raise OutputError(
            f"{path.name} was computed against a different manifest.json "
            "(the dataset was re-ingested, e.g. under another missing-value "
            "convention); regenerate relationships"
        )
    return payload


def _bounded_result_limit(limit: int) -> int:
    requested = int(limit)
    if requested < 1:
        raise ValueError("limit must be at least 1")
    return min(requested, _MAX_READ_ROWS)


def _ordered_union(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for item in group:
            if item not in seen:
                seen.add(item)
                result.append(item)
    return result


def _require_known_columns(path: str, available: list[str], selected: list[str]) -> None:
    unknown = [name for name in selected if name not in available]
    if unknown:
        raise KeyError(f"unknown column(s) for '{path}': {unknown}; available columns: {available}")


def _project_row(row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    return {
        "source_row": row.get("source_row"),
        "values": {name: row["values"].get(name) for name in columns},
        "missing": {
            name: detail for name, detail in row.get("missing", {}).items() if name in columns
        },
    }


def _project_joined_row(
    row: dict[str, Any], left_columns: list[str], right_columns: list[str]
) -> dict[str, Any]:
    right = row.get("right")
    return {
        "source_rows": row["source_rows"],
        "left": {name: row["left"].get(name) for name in left_columns},
        "right": (
            {name: right.get(name) for name in right_columns} if isinstance(right, dict) else None
        ),
    }


def _summarise(
    rows: list[dict[str, Any]],
    *,
    dtypes: dict[str, str],
    group_by: list[str],
    metrics: list[dict[str, Any]],
    filters: list[dict[str, Any]],
    unit: list[str],
    unit_metrics: list[dict[str, Any]],
    on_inconsistent_unit: str,
    unit_sample: int,
) -> dict[str, Any]:
    """The aggregation body shared by ``aggregate`` and ``aggregate_join``.

    Filters run on rows, before any unit is formed: a filter selects
    observations, and a unit is whatever the selected observations of it are.
    """
    selected = rows
    if filters:
        selected, _ = query.filter_rows(rows, filters, limit=len(rows))
    used_ops = sorted(
        {
            str(metric.get("op"))
            for metric in [*metrics, *unit_metrics]
            if isinstance(metric, dict) and metric.get("op") in query.AGGREGATES
        }
    )
    summary: dict[str, Any] = {
        "rows_included": len(selected),
        "metric_definitions": {op: query.METRIC_DEFINITIONS[op] for op in used_ops},
        "missing_values": (
            "missing cells (empty, or a sentinel under the manifest's missing-value "
            "convention) are excluded from each metric separately; count includes them, "
            "n_missing counts them, and a metric with no values left is null with a reason"
        ),
    }
    if not unit:
        summary["groups"] = query.aggregate_rows(
            selected, group_by=group_by, metrics=metrics, dtypes=dtypes
        )
        return summary
    result = query.aggregate_units(
        selected,
        group_by=group_by,
        unit=unit,
        unit_metrics=unit_metrics,
        metrics=metrics,
        dtypes=dtypes,
        on_inconsistent_unit=on_inconsistent_unit,
        unit_sample=unit_sample,
    )
    groups = result.pop("groups")
    summary["analysis_unit"] = result
    summary["groups"] = groups
    return summary


def _split_qualified(name: str) -> tuple[str, str]:
    """Split ``left.<column>`` / ``right.<column>`` at the first dot only."""
    side, dot, column = name.partition(".")
    if not dot or side not in {"left", "right"} or not column:
        raise ValueError(
            f"column reference {name!r} must be qualified as 'left.<column>' or "
            "'right.<column>' in a join aggregation"
        )
    return side, column


def _qualified_dtypes(side: str, profile: dict[str, Any], columns: list[str]) -> dict[str, str]:
    dtypes = {column["name"]: column.get("dtype", "string") for column in profile["columns"]}
    return {f"{side}.{name}": dtypes[name] for name in columns}


def _flatten_joined_row(
    row: dict[str, Any], left_columns: list[str], right_columns: list[str]
) -> dict[str, Any]:
    """One joined row as a query row with qualified column names.

    ``source_row`` keeps both locators, so every unit and every missing-key row
    in the result points at the exact rows of both files it came from.
    """
    right = row.get("right")
    values = {f"left.{name}": row["left"].get(name) for name in left_columns}
    values.update(
        {
            f"right.{name}": (right.get(name) if isinstance(right, dict) else None)
            for name in right_columns
        }
    )
    return {"source_row": dict(row["source_rows"]), "values": values}


def _missing_convention(payload: dict[str, Any]) -> MissingValueConvention:
    """Rehydrate the convention recorded in the manifest for query-time reads."""
    return MissingValueConvention(
        id=str(payload.get("id") or "recorded"),
        tokens=frozenset(str(token) for token in payload.get("tokens", [])),
        source=str(payload.get("source") or "recorded in manifest"),
        case_sensitive=bool(payload.get("case_sensitive", False)),
    )


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise OutputError(
            f"missing {path.name}; run `data2agent ingest` first (looked in {path.parent})"
        )
    return json.loads(path.read_text(encoding="utf-8"))
