# Query input and error contract

`filter_rows`, `aggregate` and `aggregate_join` publish a closed nested filter
schema. Each predicate contains `column`, `op`, and (except for missingness
operators) `value`. The operator enum is generated from the deterministic query
registry, not maintained separately in a harness prompt.

`aggregate` and `aggregate_join` also publish closed `metrics` and `unit_metrics`
items: only `op`, `column` and `name` are accepted, and any other key is rejected
before the query runs.

```json
{"column": "group", "op": "eq", "value": "control"}
```

`in` and `not_in` require an array value. `is_missing` and `is_not_missing` may
omit value. String/numeric values are not coerced. Unknown keys, including
`operator`, are rejected rather than translated or ignored. The same validation
is performed before MCP dispatch to the service, including when an SDK would
otherwise coerce or discard fields. Valid queries retain their values, source
checksums, contributor provenance and scientific semantics.

Expected deterministic query failures use `QueryValidationError`, a subclass
of both `QueryError` and `ValueError`. Existing Python callers catching
`ValueError` keep working. A table, column or crosswalk the dataset does not
have raises `QueryLookupError`, which is additionally a `KeyError`, so callers
that caught the earlier `KeyError` keep working too. Its message is the plain
text, not the `repr` a bare `KeyError` would give.

The MCP binding exposes these anticipated diagnoses. Covered, for `read_rows`,
`filter_rows`, `aggregate`, `aggregate_join` and `join_tables`:

- unknown tool arguments and missing required arguments;
- invalid filter operators, keys and value shapes, and unknown metric keys;
- `limit` and `offset` out of range;
- unknown tables, columns and crosswalks;
- unsupported join types, empty or unequal join keys, malformed key formats,
  and conflicting or incomplete `aggregate_join` join arguments;
- unqualified or unknown `left.`/`right.`/`key.` column references;
- refused many-to-many joins and joins above the complete-scan cap;
- aggregate unit and output-name mismatches.

`describe_variable` also exposes unknown-column diagnoses, and relationship
lookups expose missing or undetermined relationships (including through
`aggregate_join`). Malformed metric operators receive explicit validation errors.
The schema cache is built once from copies of SDK tools; `list_tools` returns
independent copies so in-process consumers cannot modify subsequent dispatch.

Lookups outside the query path are exposed too (#99). A name the dataset or
server does not have raises `LookupRejected`, which is a `KeyError` with a plain
text message, and the MCP binding passes that message to the client:

- a path that is not in the dataset manifest (`inspect_file`, `inspect_table`,
  and every tool that reads a file), or a container key that names a workbook,
  BORIS project or block-declared file instead of one of its tables;
- a claim id unknown to `get_evidence`;
- a path that is not a recognised metadata file in `get_metadata`;
- an unknown FAIR principle (`get_fair_principle`, `assess_fair_principles`) or
  rule (`get_fair_indicator`);
- an unknown evidence-snapshot record in `read_snapshot_record`.

Argument values outside their range raise `QueryValidationError` and are
exposed the same way: `read_snapshot_record`'s `offset` and `limit`, an unknown
`list_relationships` status, and inconsistent `assess_fair_principles` live
publication arguments.

These messages echo the caller's input shortened to 120 characters on one line,
and name only server-side identifiers (file paths, claim, principle, rule and
record ids), at most 20 of them. They never carry file or document contents.

Not covered: relationship building from configuration, and any exception that
is not one of these anticipated rejections. Those keep their plain types, and an
SDK reports them as a generic failure: an unexpected exception, including a bare
`KeyError` raised by a bug, is never shown to the client and never treated as a
validation failure. No operation is retried, repaired or replaced by another
operation.

Clients which reject arguments against the advertised schema before calling MCP
remain responsible for preserving and displaying that local rejection. Such an
attempt did not execute a Data2Agent operation. A later valid call is a separate
attempt, not evidence that the first one succeeded.

This contract is generic to dataset queries; it contains no benchmark questions,
expected answers or model-specific behavior. Tests cover all three tool schemas,
unmodified arguments, non-dispatch of invalid filters, visible semantic errors,
and preservation of valid results. Core ingestion remains dependency-free.
