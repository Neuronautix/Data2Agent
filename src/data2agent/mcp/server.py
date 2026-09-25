"""The MCP binding for Data2MCP.

Everything here is adapter code. All behaviour lives in ``DatasetService``; this
module only translates it into MCP tools and resources and applies the mode's
tool gating. Keeping the binding this thin is a design requirement, not tidiness:
the benchmark compares hosts and orchestrators, so no host-specific behaviour is
allowed to accumulate below the MCP boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .modes import DEFAULT_MODE
from .service import DatasetService

_MCP_IMPORT_HINT = (
    "the MCP server needs the 'mcp' package: pip install 'data2agent[mcp]'\n"
    "(the deterministic ingest core has no such dependency and works without it)"
)


def _server_class() -> Any:
    """Return the SDK's server class, across the 1.x/2.x rename.

    ``FastMCP`` became ``MCPServer`` in mcp 2.x. The decorator API we rely on is
    the same in both, so supporting either costs one import and keeps Data2Agent
    usable on whichever SDK a host already has pinned.
    """
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP

        return FastMCP
    except ImportError as error:  # pragma: no cover - depends on optional extra
        raise ImportError(_MCP_IMPORT_HINT) from error


def build_server(service: DatasetService, *, name: str = "data2agent") -> Any:
    """Build an MCP server exposing the tools this service's mode allows."""
    server = _server_class()(name)

    # -- resources ----------------------------------------------------------
    #
    # Registered per mode, exactly as the tools are. Registering them all and
    # gating only the tools would let a raw-mode client enumerate and read
    # dataset://manifest and dataset://evidence -- handing the control condition
    # the structured condition and quietly invalidating the comparison.

    def manifest() -> str:
        """The dataset manifest: files, checksums, formats, table profiles."""
        return service.resource("dataset://manifest")

    def provenance() -> str:
        """How, when and with what version this dataset was ingested."""
        return service.resource("dataset://provenance")

    def evidence() -> str:
        """The full claim -> evidence ledger for this dataset."""
        return service.resource("dataset://evidence")

    def metadata() -> str:
        """The metadata files recognised in this dataset."""
        return service.resource("dataset://metadata")

    def relationship_resource() -> str:
        """Resolved relationship bundle, or an explicit not-determined record."""
        return service.resource("dataset://relationships")

    def file_resource(path: str) -> str:
        """One file's manifest record, integrity status and bounded preview."""
        return service.resource(f"dataset://files/{path}")

    resources = {
        "dataset://manifest": manifest,
        "dataset://provenance": provenance,
        "dataset://evidence": evidence,
        "dataset://metadata": metadata,
        "dataset://relationships": relationship_resource,
        "dataset://files/{path}": file_resource,
    }
    for uri in service.available_resources():
        implementation = resources.get(uri)
        if implementation is None:  # pragma: no cover - guarded by the mode registry
            raise KeyError(f"mode '{service.mode.name}' requests unknown resource '{uri}'")
        server.resource(uri)(implementation)

    # -- tools --------------------------------------------------------------

    def dataset_inventory() -> dict[str, Any]:
        """Summarise the dataset: identity, file count, formats, warnings.

        Start here. Nothing in the summary is inferred; an empty field means the
        value was not determined, never that it is absent from the dataset.
        """
        return service.dataset_inventory()

    def list_files(
        pattern: str | None = None, file_format: str | None = None
    ) -> list[dict[str, Any]]:
        """List inventoried files, optionally filtered by glob pattern and/or format."""
        return service.list_files(pattern=pattern, file_format=file_format)

    def inspect_file(path: str, preview_bytes: int = 4096) -> dict[str, Any]:
        """Inspect one file: size, checksum, detected format, and a bounded preview.

        The preview is withheld if the file no longer matches its manifest checksum.
        """
        return service.inspect_file(path, preview_bytes=preview_bytes)

    def inspect_table(path: str) -> dict[str, Any]:
        """Return a delimited table's shape: rows, columns, observed types, missingness.

        Column types describe the *shape of the observed tokens*, not the
        scientific meaning of the column. Null-like tokens such as 'NA' are
        counted separately from empty cells and are not treated as missing.
        """
        return service.inspect_table(path)

    def list_tables() -> dict[str, Any]:
        """List every profiled delimited table and workbook worksheet.

        This returns table identity and shape only. Use read_rows for the actual
        observations.
        """
        return service.list_tables()

    def read_rows(
        path: str,
        columns: list[str] | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read a bounded slice of actual observations from a profiled table.

        Values come from the immutable source bytes after checksum verification.
        Missing sentinels are normalised under the same convention used at
        ingest, with the original sentinel retained in the row's missing map.
        No semantic interpretation or analysis is performed.
        """
        return service.read_rows(path, columns=columns, offset=offset, limit=limit)

    def filter_rows(
        path: str,
        filters: list[dict[str, Any]],
        columns: list[str] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Filter observations with a closed operator registry.

        Supported operators are deterministic data comparisons only; no Python,
        SQL, regex execution, or free-form expression language is accepted.
        """
        return service.filter_rows(path, filters=filters, columns=columns, limit=limit)

    def aggregate(
        path: str,
        metrics: list[dict[str, Any]],
        group_by: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compute bounded deterministic summaries over a complete table scan.

        Metrics are restricted to count, n_missing, sum, mean, min and max.
        The call refuses a table above the complete-scan safety cap rather than
        returning a partial statistic that looks complete.
        """
        return service.aggregate(path, group_by=group_by, metrics=metrics, filters=filters)

    def describe_variable(path: str, column: str) -> dict[str, Any]:
        """Describe one observed column without assigning scientific meaning to it."""
        return service.describe_variable(path, column)

    def join_tables(
        left: str,
        right: str,
        left_keys: list[str],
        right_keys: list[str],
        left_columns: list[str] | None = None,
        right_columns: list[str] | None = None,
        how: str = "inner",
        limit: int = 100,
        crosswalk: str | None = None,
        left_key_format: str | None = None,
        right_key_format: str | None = None,
    ) -> dict[str, Any]:
        """Join tables only on keys explicitly supplied by the caller.

        Key uniqueness and cardinality are diagnosed and returned. No
        relationship is inferred or promoted by this operation. Keys match by
        exact equality unless `crosswalk` names an identifier crosswalk already
        declared in relationships.json (list_relationships shows them); you
        cannot supply mappings yourself. Through a crosswalk, each row returns
        both sides' raw key values plus the canonical ID compared; values absent
        from the crosswalk pass through unchanged and are counted, and a table
        writing one canonical ID in two forms is reported as a collision with
        rows withheld. `left_key_format`/`right_key_format` (e.g. "{cage}-{tail}")
        render a composite key from that side's own key columns.
        """
        return service.join_tables(
            left,
            right,
            left_keys=left_keys,
            right_keys=right_keys,
            left_columns=left_columns,
            right_columns=right_columns,
            how=how,
            limit=limit,
            crosswalk=crosswalk,
            left_key_format=left_key_format,
            right_key_format=right_key_format,
        )

    def list_relationships(status: str | None = None) -> dict[str, Any]:
        """List saved cross-table relationships with epistemic status and evidence.

        Candidate relationships are structural suggestions only. They are not
        equivalent to declared or deterministic relationships. A record with
        `key_mapping` was assessed through a declared identifier crosswalk (cited
        by name and sha256): its cardinality and overlap are on canonical IDs, and
        it counts mapped, unmapped and unmatched key values per side. The
        bundle's declared crosswalks are listed under `crosswalks`.
        """
        return service.list_relationships(status)

    def get_relationship(relationship_id: str) -> dict[str, Any]:
        """Return one saved relationship record by stable identifier."""
        return service.get_relationship(relationship_id)

    def join_relationship(
        relationship_id: str,
        left_columns: list[str] | None = None,
        right_columns: list[str] | None = None,
        how: str = "inner",
        limit: int = 100,
    ) -> dict[str, Any]:
        """Execute a saved declared/deterministic relationship as a join contract.

        Candidate or rejected relationships are refused. This prevents a
        plausible structural overlap from silently becoming a scientific fact.
        A relationship declared through an identifier crosswalk is joined through
        that exact crosswalk; each row carries both raw key values and the
        canonical ID, and the contract cites the crosswalk's name and sha256.
        """
        return service.join_relationship(
            relationship_id,
            left_columns=left_columns,
            right_columns=right_columns,
            how=how,
            limit=limit,
        )

    def get_metadata(path: str | None = None) -> dict[str, Any]:
        """List recognised metadata files, or return one of them verbatim."""
        return service.get_metadata(path)

    def get_evidence(
        claim_id: str | None = None,
        subject: str | None = None,
        check: str | None = None,
        contains: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Retrieve the evidence behind a claim: the check run, and the bytes it ran on.

        Use this before asserting anything about the dataset. If no claim
        supports a statement, the statement is unsupported -- say so rather than
        filling the gap.
        """
        return service.get_evidence(
            claim_id=claim_id, subject=subject, check=check, contains=contains, limit=limit
        )

    def resolve_identifier(value: str) -> dict[str, Any]:
        """Find where an identifier occurs in the dataset. No network resolution is attempted.

        When relationships.json declares identifier crosswalks, also reports
        exact (case-sensitive) crosswalk membership: the canonical ID a written
        form maps to and all its listed forms. Membership is a declaration by the
        crosswalk's author, not an observation.
        """
        return service.resolve_identifier(value)

    def get_provenance() -> dict[str, Any]:
        """When, where and with what version this dataset was ingested.

        Timestamps live here rather than in the manifest, so that repeated
        ingests of identical bytes still produce identical manifests.
        """
        return service.get_provenance()

    def list_fair_rules() -> dict[str, Any]:
        """List the canonical FAIR rules: id, principle, question, implementation status."""
        return service.list_fair_rules()

    def get_fair_indicator(rule_id: str) -> dict[str, Any]:
        """Return one canonical FAIR rule in full: its question, check, and allowed results.

        'unknown' is always among the allowed results. A rule that cannot report
        uncertainty would manufacture certainty instead.
        """
        return service.get_fair_indicator(rule_id)

    def run_fair_check(
        rule_id: str | None = None,
        host: str | None = None,
        model: str | None = None,
        orchestrator: str | None = None,
    ) -> dict[str, Any]:
        """Run the deterministic FAIR checks, returning an evidence-bound assessment.

        Verdicts come from code, not from a model. Results marked 'unknown' are
        preserved as unknown and must not be resolved by reasoning over them.
        """
        return service.run_fair_check(rule_id, host=host, model=model, orchestrator=orchestrator)

    def validate_identifier(value: str) -> dict[str, Any]:
        """Validate an identifier's syntax against its scheme. Makes no network request.

        A syntactically valid identifier is not a resolvable one; the response
        says so, and that distinction must be preserved when reporting.
        """
        return service.validate_identifier(value)

    implementations = {
        "dataset_inventory": dataset_inventory,
        "list_files": list_files,
        "inspect_file": inspect_file,
        "inspect_table": inspect_table,
        "list_tables": list_tables,
        "read_rows": read_rows,
        "filter_rows": filter_rows,
        "aggregate": aggregate,
        "describe_variable": describe_variable,
        "join_tables": join_tables,
        "list_relationships": list_relationships,
        "get_relationship": get_relationship,
        "join_relationship": join_relationship,
        "get_metadata": get_metadata,
        "get_evidence": get_evidence,
        "resolve_identifier": resolve_identifier,
        "get_provenance": get_provenance,
        "list_fair_rules": list_fair_rules,
        "get_fair_indicator": get_fair_indicator,
        "run_fair_check": run_fair_check,
        "validate_identifier": validate_identifier,
    }
    for tool_name in service.available_tools():
        implementation = implementations.get(tool_name)
        if implementation is None:  # pragma: no cover - guarded by modes.resolve_mode
            raise KeyError(f"mode '{service.mode.name}' requests unimplemented tool '{tool_name}'")
        server.tool(name=tool_name)(implementation)

    return server


def serve(
    output_dir: Path,
    *,
    source_dir: Path | None = None,
    mode: str = DEFAULT_MODE,
    transport: str = "stdio",
) -> None:
    """Run a Data2MCP server over an ingested dataset."""
    service = DatasetService(output_dir, source_dir=source_dir, mode=mode)
    build_server(service).run(transport=transport)
