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

Not covered: tools outside the query path (relationship building and listing,
FAIR assessment, metadata and evidence lookups, `inspect_table`'s `KeyError`
for an unknown path) still raise plain exceptions, which an SDK may report as a
generic failure. Arbitrary unexpected exceptions are never treated as query
validation failures. No operation is retried, repaired or replaced by another
operation.

Clients which reject arguments against the advertised schema before calling MCP
remain responsible for preserving and displaying that local rejection. Such an
attempt did not execute a Data2Agent operation. A later valid call is a separate
attempt, not evidence that the first one succeeded.

This contract is generic to dataset queries; it contains no benchmark questions,
expected answers or model-specific behavior. Tests cover all three tool schemas,
unmodified arguments, non-dispatch of invalid filters, visible semantic errors,
and preservation of valid results. Core ingestion remains dependency-free.
