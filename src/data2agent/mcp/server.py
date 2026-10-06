"""The MCP binding for Data2MCP.

Everything here is adapter code. All behaviour lives in ``DatasetService``; this
module only translates it into MCP tools and resources and applies the mode's
tool gating. Keeping the binding this thin is a design requirement, not tidiness:
the benchmark compares hosts and orchestrators, so no host-specific behaviour is
allowed to accumulate below the MCP boundary.
"""

from __future__ import annotations

from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Any

from ..errors import QueryError
from ..query.operations import AGGREGATES, FILTER_OPERATORS, _validate_filters
from .modes import DEFAULT_MODE
from .service import DatasetService

_MCP_IMPORT_HINT = (
    "the MCP server needs the 'mcp' package: pip install 'data2agent[mcp]'\n"
    "(the deterministic ingest core has no such dependency and works without it)"
)


def _base_server_class() -> Any:
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


def _tool_error_class() -> Any:
    """Return ToolError across the MCP SDK 1.x/2.x package rename."""
    try:
        from mcp.server.mcpserver.exceptions import ToolError

        return ToolError
    except ImportError:
        from mcp.server.fastmcp.exceptions import ToolError

        return ToolError


def _expose_query_errors(function: Any) -> Any:
    """Expose anticipated query rejections without leaking unexpected crashes."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except QueryError as error:
            raise _tool_error_class()(str(error)) from None

    return wrapped


def _tool_input_schema(tool: Any) -> dict[str, Any]:
    """Return the mutable input schema across MCP SDK 1.x/2.x field naming."""

    for name in ("input_schema", "inputSchema"):
        schema = getattr(tool, name, None)
        if isinstance(schema, dict):
            return schema
    raise RuntimeError("MCP tool does not expose an input schema")


_FILTER_TOOLS = frozenset({"filter_rows", "aggregate", "aggregate_join"})
_METRIC_TOOLS = frozenset({"aggregate", "aggregate_join"})


def _array_schema(property_schema: dict[str, Any]) -> dict[str, Any]:
    """The array branch of an optional-or-array property schema."""
    return next(
        (item for item in property_schema.get("anyOf", []) if item.get("type") == "array"),
        property_schema,
    )


def _filter_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["column", "op"],
        "allOf": [
            {
                "if": {"properties": {"op": {"enum": ["is_missing", "is_not_missing"]}}},
                "else": {"required": ["value"]},
            },
            {
                "if": {"properties": {"op": {"enum": ["in", "not_in"]}}},
                "then": {"properties": {"value": {"type": "array"}}},
            },
        ],
        "properties": {
            "column": {"type": "string", "minLength": 1},
            "op": {"type": "string", "enum": sorted(FILTER_OPERATORS)},
            "value": {
                "description": "Required except for is_missing/is_not_missing; "
                "in/not_in require an array. Values are compared without coercion."
            },
        },
    }


def _metric_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["op"],
        "properties": {
            "op": {"type": "string", "enum": sorted(AGGREGATES)},
            "column": {
                "type": ["string", "null"],
                "description": "Required for every op except count, which takes none.",
            },
            "name": {"type": "string", "description": "Output name; defaults to op[:column]."},
        },
    }


def _close_tool_schema(tool: Any) -> None:
    """Close one tool's published schema: no unknown arguments, filter or metric keys."""
    schema = _tool_input_schema(tool)
    schema["additionalProperties"] = False
    properties = schema.get("properties", {})
    if tool.name in _FILTER_TOOLS:
        _array_schema(properties["filters"])["items"] = _filter_item_schema()
    if tool.name in _METRIC_TOOLS:
        for key in ("metrics", "unit_metrics"):
            if key in properties:
                _array_schema(properties[key])["items"] = _metric_item_schema()


def _server_class() -> Any:
    """Return a Data2Agent server that keeps published and executed args identical."""

    base = _base_server_class()

    class Data2AgentServer(base):
        # Closed once, on first use: build_server registers every tool before the
        # server is served, so the published schemas never need closing again and
        # call_tool never mutates shared SDK state.
        _closed_tools: dict[str, Any] | None = None

        async def _tools_by_name(self) -> dict[str, Any]:
            if self._closed_tools is None:
                tools = deepcopy(await super().list_tools())
                for tool in tools:
                    _close_tool_schema(tool)
                self._closed_tools = {tool.name: tool for tool in tools}
            return self._closed_tools

        async def list_tools(self) -> list[Any]:
            return deepcopy(list((await self._tools_by_name()).values()))

        async def call_tool(
            self, name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any
        ) -> Any:
            if isinstance(arguments, dict):
                tool = (await self._tools_by_name()).get(name)
                if tool is not None:
                    ToolError = _tool_error_class()
                    schema = _tool_input_schema(tool)
                    properties = schema.get("properties")
                    allowed = set(properties) if isinstance(properties, dict) else set()
                    unknown = sorted(set(arguments) - allowed)
                    if unknown:
                        raise ToolError(
                            f"unknown argument(s) for tool '{name}': {unknown}; "
                            f"allowed arguments: {sorted(allowed)}"
                        )
                    missing = [key for key in schema.get("required", []) if key not in arguments]
                    if missing:
                        raise ToolError(
                            f"missing required argument(s) for tool '{name}': {missing}"
                        )
                    if name in _FILTER_TOOLS:
                        filters = arguments.get("filters")
                        if filters is not None or name == "filter_rows":
                            try:
                                _validate_filters(filters)
                            except QueryError as error:
                                raise ToolError(str(error)) from None
            return await super().call_tool(name, arguments, *args, **kwargs)

    return Data2AgentServer


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

    def inspect_table(
        path: str,
        include_rows_above_data: bool = False,
        max_rows: int = 20,
        max_cells: int = 64,
    ) -> dict[str, Any]:
        """Return a table's shape: rows, columns, observed types, missingness.

        Column types describe the *shape of the observed tokens*, not the
        scientific meaning of the column. Null-like tokens such as 'NA' are
        counted separately from empty cells and are not treated as missing.

        header_row / header_source say which row named the columns and how it
        was chosen. rows_above_data_available counts rows above the data that
        the column names do not show (a skipped banner or preamble, or the rows
        of a multi-row header). Pass include_rows_above_data=true to read their
        non-empty cells from the checksum-verified file, bounded by max_rows and
        max_cells, each with its row number and column letter.
        """
        return service.inspect_table(
            path,
            include_rows_above_data=include_rows_above_data,
            max_rows=max_rows,
            max_cells=max_cells,
        )

    def list_tables() -> dict[str, Any]:
        """List every profiled table: delimited files, worksheets, and BORIS projects.

        A BORIS project (.boris) yields three tables: '<file>#events' (one row
        per coded event), '<file>#intervals' (state events paired start/stop
        by BORIS's toggle rule, point events as zero-length intervals, an
        unclosed start kept with Pairing 'unmatched_start' and a null
        duration) and '<file>#observations' (one row per observation).

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

        This tool reads by offset/limit only. It does not apply predicates or
        filter conditions. When rows must satisfy conditions on column values,
        use filter_rows instead.

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

        Use this tool whenever returned rows must satisfy one or more conditions
        on column values. Unlike read_rows, this tool applies the supplied
        predicates before returning observations.

        Supported operators are deterministic data comparisons only; no Python,
        SQL, regex execution, or free-form expression language is accepted.
        Each filter uses column, op, value, e.g.
        {"column": "group", "op": "eq", "value": "control"}.
        Omit value only for is_missing/is_not_missing. in/not_in need an array.
        """
        return service.filter_rows(path, filters=filters, columns=columns, limit=limit)

    def aggregate(
        path: str,
        metrics: list[dict[str, Any]],
        group_by: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        unit: list[str] | None = None,
        unit_metrics: list[dict[str, Any]] | None = None,
        on_inconsistent_unit: str = "refuse",
        unit_sample: int = 50,
    ) -> dict[str, Any]:
        """Compute bounded deterministic summaries over a complete table scan.

        Metrics come from a closed registry: count, n_present, n_missing,
        n_distinct, sum, mean, min, max, median, sd (sample, n-1) and sem.
        Definitions are returned with every result; a null metric carries a reason.

        If rows are repeated measures (time bins, trials), declare the
        experimental unit, e.g. unit=["animal_id", "day"] with
        unit_metrics=[{"op": "sum", "column": "duration"}]. Rows are first reduced
        to one record per unit, then metrics summarise units: their columns name
        unit_metrics outputs ("sum:duration") and count counts units. Each group
        reports n_units, n_rows and the contributing unit ids. A unit whose rows
        disagree on a group_by value refuses the result unless
        on_inconsistent_unit="exclude". Without a unit, every row counts once.

        The call refuses a table above the complete-scan safety cap rather than
        returning a partial statistic that looks complete.
        """
        return service.aggregate(
            path,
            group_by=group_by,
            metrics=metrics,
            filters=filters,
            unit=unit,
            unit_metrics=unit_metrics,
            on_inconsistent_unit=on_inconsistent_unit,
            unit_sample=unit_sample,
        )

    def aggregate_join(
        metrics: list[dict[str, Any]],
        relationship_id: str | None = None,
        left: str | None = None,
        right: str | None = None,
        left_keys: list[str] | None = None,
        right_keys: list[str] | None = None,
        how: str = "inner",
        crosswalk: str | None = None,
        left_key_format: str | None = None,
        right_key_format: str | None = None,
        group_by: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        unit: list[str] | None = None,
        unit_metrics: list[dict[str, Any]] | None = None,
        on_inconsistent_unit: str = "refuse",
        unit_sample: int = 50,
    ) -> dict[str, Any]:
        """Aggregate over a complete join, e.g. group measurements by a registry column.

        Name the join by a declared relationship_id (its saved keys, key formats
        and identifier crosswalk are used exactly as join_relationship uses
        them), or give left, right, left_keys and right_keys explicitly, with
        optionally a crosswalk declared in relationships.json by name and
        left/right_key_format. Every column reference (group_by, unit, metrics,
        filters) is qualified as "left.<column>" or "right.<column>".

        Through a crosswalk, "key.canonical_id" names the canonical ID of the
        join key (null when unmapped), so unit=["key.canonical_id", ...] reduces
        per canonical animal whatever each file's spelling; units then list the
        raw forms seen. Also "key.mapping", "key.left_form", "key.right_form".
        Rendering or crosswalk collisions refuse the aggregation.

        Metrics, units and missing values behave exactly as in aggregate.
        Many-to-many joins are refused; both backing files and the crosswalk
        are cited, with join cardinality, key-mapping facts and the mapped /
        unmapped / unmatched counts of the rows aggregated.
        """
        return service.aggregate_join(
            metrics=metrics,
            relationship_id=relationship_id,
            left=left,
            right=right,
            left_keys=left_keys,
            right_keys=right_keys,
            how=how,
            crosswalk=crosswalk,
            left_key_format=left_key_format,
            right_key_format=right_key_format,
            group_by=group_by,
            filters=filters,
            unit=unit,
            unit_metrics=unit_metrics,
            on_inconsistent_unit=on_inconsistent_unit,
            unit_sample=unit_sample,
        )

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
        If a side was declared `forms_per_canonical: "many"` (several written
        forms per subject in that table, e.g. one observation id per session),
        the contract says so; cardinality is then per canonical ID, and
        key_mapping.<side>.form_level gives the per-form counts.
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

    def list_fair_principles() -> dict[str, Any]:
        """List all 15 GO FAIR Foundation interpretation items and local rule links."""
        return service.list_fair_principles()

    def get_fair_principle(principle_id: str) -> dict[str, Any]:
        """Get one FAIR item's question, required evidence, source, and next action."""
        return service.get_fair_principle(principle_id)

    def assess_fair_principles(
        principle_id: str | None = None,
        live: bool = False,
        publication_url: str | None = None,
        unpublished: bool = False,
    ) -> dict[str, Any]:
        """Assess Foundation items and recommend actions for unverified evidence.

        Set live=true with a public publication_url to probe its HTTP route.
        The public DOI resolver exchange is recorded as a scoped observation;
        it cannot establish dataset identity or a full principle pass.
        """
        return service.assess_fair_principles(
            principle_id, live=live, publication_url=publication_url, unpublished=unpublished
        )

    def get_fair_recommendations(
        rule_id: str | None = None, unpublished: bool = False
    ) -> dict[str, Any]:
        """Get local finding repairs plus evidence gaps for all 15 FAIR items."""
        return service.get_fair_recommendations(rule_id, unpublished=unpublished)

    def plan_fair_publication(
        purpose: str = "explore",
        owner_approval: bool | None = None,
        metadata_public_approved: bool | None = None,
        files_reviewed: bool | None = None,
        files_public_approved: bool | None = None,
        test_with_real_data: bool = False,
        community_submission: bool = False,
        embargo_until: str | None = None,
    ) -> dict[str, Any]:
        """Compare Zenodo draft, sandbox, restricted, public, and embargo paths.

        Answers express user decisions; the tool checks current local FAIR
        findings and asks for missing owner decisions. It never contacts Zenodo
        or publishes the dataset. Use purpose=explore, test, or release.
        """
        return service.plan_fair_publication(
            purpose=purpose,
            owner_approval=owner_approval,
            metadata_public_approved=metadata_public_approved,
            files_reviewed=files_reviewed,
            files_public_approved=files_public_approved,
            test_with_real_data=test_with_real_data,
            community_submission=community_submission,
            embargo_until=embargo_until,
        )

    def run_fair_check(
        rule_id: str | None = None,
        host: str | None = None,
        model: str | None = None,
        orchestrator: str | None = None,
    ) -> dict[str, Any]:
        """Run narrow local FAIR checks, returning an evidence-bound assessment.

        Verdicts come from code, not from a model. Results marked 'unknown' are
        preserved as unknown and must not be resolved by reasoning over them.
        A pass is not a full Foundation-principle pass; use
        assess_fair_principles for the 15 interpretation-level items.
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
        "aggregate_join": aggregate_join,
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
        "list_fair_principles": list_fair_principles,
        "get_fair_principle": get_fair_principle,
        "assess_fair_principles": assess_fair_principles,
        "get_fair_recommendations": get_fair_recommendations,
        "plan_fair_publication": plan_fair_publication,
        "run_fair_check": run_fair_check,
        "validate_identifier": validate_identifier,
    }
    for tool_name in service.available_tools():
        implementation = implementations.get(tool_name)
        if implementation is None:  # pragma: no cover - guarded by modes.resolve_mode
            raise KeyError(f"mode '{service.mode.name}' requests unimplemented tool '{tool_name}'")
        server.tool(name=tool_name)(_expose_query_errors(implementation))

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
