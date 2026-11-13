# Changelog

All notable changes to Data2Agent are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Versioning

- **Package versions** (`0.3.0`) follow [Semantic Versioning](https://semver.org/) and
  are declared in `pyproject.toml` and `src/data2agent/__init__.py`. A test
  (`tests/test_version.py`) keeps the two equal and requires a heading for the
  current version in this file.
- **Roadmap milestones** (`v0.1` ... `v0.6` in [`docs/ROADMAP.md`](docs/ROADMAP.md)) name groups of
  planned features. They are a separate numbering and are **not** package versions.
  Package `0.3.0` does not mean the roadmap's `v0.3` (prose projection, curated
  export, remote sources) has shipped; it has not.
- **Format versions** (`d2a-...-N`, `snapshot_format_version`, `manifest_version`, ...)
  version one document format each, independently of the package. See the
  [compatibility policy](docs/semantic-layer.md#format-versions-and-compatibility-policy)
  for the optional semantic and CEDAR formats.

## [0.3.0] - 2026-11-13

Adds the optional semantic layer, an offline CEDAR interoperability boundary, and
two MCP/snapshot refinements. Everything new is opt-in. The deterministic
ingestion core, the manifest and evidence formats, the FAIR profile and every
mode's default tool list are unchanged: ingesting `examples/preclinical-minimal`
with this release and with `v0.2.0` produces byte-identical `manifest.json` and
`evidence.json`.

### Added

- **Optional semantic layer** (#101). Runtime ontology constraints over a pinned
  ontology snapshot and a closed-world mapping policy; a sidecar mapping record
  (`data2agent semantic`); and retrieval tools for `serve` behind
  `--semantic-snapshot` / `--semantic-policy` (`get_mapping_constraints`,
  `search_ontology_terms`, `get_term_context`), with `validate_semantic_mapping` and
  `get_semantic_mapping` only when named in `--semantic-tools`. Off by default;
  `fair-semantic` is still not implemented. Experimental. See
  [`docs/semantic-layer.md`](docs/semantic-layer.md).
- **Scheme-aware snapshots and policies** (#106). `d2a-ontology-snapshot-2` records SKOS
  schemes and each concept's `skos:inScheme` membership, and keeps SKOS `broader`
  apart from class subclass relations; `d2a-mapping-policy-2` supports
  `allowed.schemes` (with `term_kinds: [concept]`) and requires a snapshot-2 snapshot.
  Opt in with `snapshot_schema_version` in the build spec. **The default is still
  `d2a-ontology-snapshot-1`, and its output is unchanged** (the compiled example
  snapshot in `examples/semantic/` is byte-identical to the one in the first
  release of the semantic layer, and a test pins that).
- **Offline CEDAR conversion boundary** (#106), library API only
  (`data2agent.semantic.cedar`):
  - `compile_profile` verifies each source path, version and SHA-256 of the HCM
    Recording Minimal v1 profile, then emits a `d2a-cedar-conversion-1` report
    (`schemas/d2a-cedar-conversion-1.schema.json`) with a local candidate snapshot
    and policy and a per-field account of what could and could not be converted.
  - `import_template` reads a CEDAR template (JSON) into a candidate policy, behind a
    pinned source and an approval digest.
  - `export_class_policy` returns the original pinned CEDAR object only when a
    re-import proves nothing changed.
- **Partial offline CEDAR draft export** (#107). `export_profile_draft` returns a
  `d2a-cedar-draft-export-1` document (`schemas/d2a-cedar-draft-export-1.schema.json`):
  single string and date fields, and the sampling-rate QUDT structure, projected
  as a CEDAR editing draft. Identity and provenance are null and
  `publication_status` is always `blocked`. The original rules are kept in a
  machine-readable `enforcement` block, and omitted fields are listed.
- **Two optional extras** in `pyproject.toml`:

  | Extra | Installs | For |
  | --- | --- | --- |
  | `semantic-build` | `rdflib` | compiling an ontology snapshot from RDF artifacts (build time only; the runtime reads compiled JSON with the standard library) |
  | `cedar` | `PyYAML`, `rdflib` | the CEDAR conversion and draft-export APIs |

  ```bash
  pip install 'data2agent[cedar] @ git+https://github.com/Neuronautix/Data2Agent@v0.3.0'
  pip install 'data2agent[semantic-build] @ git+https://github.com/Neuronautix/Data2Agent@v0.3.0'
  # or, from a checkout
  pip install -e '.[mcp,fair,cedar]'
  ```

  Without an extra, the core and the semantic layer's runtime still import and run;
  only the code paths above need it. CI now installs the `cedar` extra.
- **`truncation_reason` on evidence snapshot records** (#103): `body_limit`,
  `partial_range`, `range_inconsistent`, or `null`. `truncated` itself keeps its
  fail-closed meaning.
- `data2agent --version`, `provenance.json` and the MCP handshake now report
  `0.3.0` (see the note under 0.2.0).

### Changed

- **Evidence snapshot format `1.0` -> `1.1`** (#103). New `data2agent snapshot` runs
  write `snapshot_format_version: "1.1"`, whose only difference is
  `truncation_reason`. `serve` still reads `1.0` snapshots exactly as collected (their
  checksums do not change); a truncated `1.0` record is served with
  `truncation_reason: "not_recorded"`, never `null`. **A `1.1` snapshot cannot be
  read by `v0.2.0`**, which refuses any version other than `1.0`: a runtime that is
  pinned to an older Data2Agent must be re-pinned before it serves a newly collected
  snapshot. The served `list_snapshot_records` listing and tool description also
  gain the new field.
- **MCP clients now see the message of an anticipated lookup miss** (#104), for
  example an unknown path, claim id, metadata file, FAIR principle or rule, or
  snapshot record, instead of the SDK's generic `Error executing tool`. The new
  `LookupRejected` is still a `KeyError`; an out-of-range `read_snapshot_record`
  argument is now a `QueryValidationError`, still a `ValueError`. Messages echo
  caller input shortened to 120 characters and name at most 20 server-side
  identifiers; they never carry file contents. Unexpected exceptions stay masked.
- The descriptions of the not-yet-implemented `fair-skill` and `fair-semantic` modes now
  read "Planned (not yet implemented)" instead of naming a roadmap version, so that package
  `0.3.0` does not suggest those features have shipped. Wording only: no mode, tool list
  or behaviour changes.

### Not supported

Stated so that nobody reads more into this release than it contains.

- **No CLI for the CEDAR boundary.** `compile_profile`, `export_profile`,
  `export_profile_draft`, `import_template` and `export_class_policy` are library
  calls. (`data2agent semantic` and the `serve` flags above are for the semantic
  layer, not CEDAR.)
- **`import_template` accepts explicit, single-valued ontology-class lists only.**
  Branches, ontology-wide scopes, value sets, exclusions, repeated or required fields,
  defaults, literals, nested structures and unknown constraints are blocked, not
  translated. JSON only; there is no YAML template import.
- **No profile -> CEDAR -> policy round trip.** Exporting a profile draft and importing
  it back is not supported, and `export_class_policy` cannot synthesise a CEDAR
  template; it only recovers an unchanged, pinned original.
- **Full-profile export is blocked.** `export_profile` still returns a blocked report
  and no artifact. The draft export is partial: it omits time, duration, SKOS-concept
  and unpinned taxon fields, and every draft is `publication_status: blocked`. Do not
  publish from either report.
- A candidate policy checks that a value is a *permitted term*. It does not check
  record structure, provenance, expert review or scientific correctness; those need
  the independent profile validators.
- `fair-semantic` is not enabled and no SHACL engine is bundled.
- No provider client, no mutable-URL terminology lookup, no network access.
- **No claim is made** about the efficacy, accuracy or scientific fitness of any
  mapping, policy or draft produced by these features, and none has been evaluated
  on real data. The example ontology snapshot is a hand-transcribed extract for
  tests, not a release.

### Compatibility

- Existing users: no change is needed. Ingestion, the manifest, evidence ledger,
  assessment and recommendation formats, the FAIR profile, the CLI commands and the
  default tool list of every mode are unchanged. The two behavioural changes
  (snapshot `1.1`, visible lookup errors) are described above.
- New format identifiers in this release: `d2a-ontology-snapshot-2`,
  `d2a-mapping-policy-2`, `d2a-cedar-conversion-1`, `d2a-cedar-draft-export-1`.
  First-release identifiers of the semantic layer: `d2a-ontology-snapshot-1`,
  `d2a-mapping-policy-1`, `d2a-snapshot-build-1`, `d2a-semantic-bindings-1`,
  `d2a-semantic-mapping-1`, `d2a-semantic-inferred-1`.
- Format compatibility promises: see the
  [compatibility policy](docs/semantic-layer.md#format-versions-and-compatibility-policy).

## [0.2.0] - 2026-11-03

Tagged at `65884a7` ("Freeze external FAIR evidence once and serve it offline", #98) and
published as a GitHub release on 2026-11-07, without release notes. FAIR profile,
rule registry and deterministic FAIR checks (the roadmap's `v0.2`), plus the work
merged up to that commit; see `git log v0.2.0`.

**Version metadata.** The `v0.2.0` tag was created without bumping `pyproject.toml` or
`data2agent.__version__`, both of which still read `0.1.0`. A build installed from
that tag therefore reports itself as `0.1.0` (`data2agent --version`, `provenance.json`,
the MCP `serverInfo.version`; the latter two also carry the commit, e.g.
`0.1.0+g65884a7b49fe`, which is what identifies the build). The tag is not
rewritten. `0.3.0` is the first release whose package metadata matches its tag.

[0.3.0]: https://github.com/Neuronautix/Data2Agent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Neuronautix/Data2Agent/releases/tag/v0.2.0
