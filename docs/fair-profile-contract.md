# FAIR profile contract

FAIR is **not** hard-coded into Data2MCP. It is a profile that sits on the core:

```text
Data2Agent
│
├── Core
│   ├── ingestion      deterministic, no FAIR vocabulary anywhere
│   ├── evidence       claim → evidence → bytes
│   └── MCP            generic dataset tools
│
└── Profiles
    └── fair/          rules · schemas · vocabularies · shacl · checks
```

Keeping `Data2MCP ≠ FAIR checker` is the experimental separation the study
depends on: it is what lets `structured` and `fair-*` differ in exactly one
thing. If FAIR concepts leak into the core, the control condition stops being a
control.

**Status: specified, not implemented.** No `profiles/` package exists in v0.1,
on purpose — an empty package that promises behaviour is worse than a backlog
entry that schedules it. What follows is the contract that v0.2 must satisfy.

## The canonical rule

One rule, one file, one identifier. Everything downstream is a *projection* of
this record, never a reimplementation of it:

```yaml
id: F1-PID-METADATA
principle: F1

question: >
  Does the metadata have a globally unique persistent identifier?

check:
  type: identifier_resolution
  inputs:
    - manifest.identifiers
    - metadata_files

allowed_results:
  - pass
  - fail
  - unknown
  - not_applicable

inference_allowed: false

evidence_required: true

rationale_required_for:
  - fail
  - unknown
```

From that single record we generate:

| Projection | Consumed by |
| --- | --- |
| a Markdown Skill section | `mode: fair-skill` |
| a structured rule listing | `mode: fair-rules` |
| a deterministic check implementation | `mode: fair-deterministic` |
| a JSON Schema fragment for the result | output constraint |
| SHACL shapes | `mode: fair-semantic` |
| an MCP tool (`run_fair_check`) | every `fair-*` mode |

One canonical source with six projections is precisely what the constraint
experiment needs: the *content* is held constant while the *form of the
constraint* varies. Six independently written rule sets would confound form with
content and make the comparison meaningless.

## Non-negotiable rule semantics

1. **`unknown` is a first-class result.** A rule that cannot return `unknown`
   will eventually invent an answer. `not_applicable` is distinct from `fail`.
2. **`inference_allowed: false` is the default.** A rule may only set it true
   with a written justification in the rule file.
3. **Every result carries evidence**, `unknown` included — an `unknown` must
   still say what was checked and came back empty.
4. **A rule may not read the dataset directly.** It consumes manifest fields,
   evidence claims, and metadata files served by the core. This keeps every FAIR
   verdict anchored to checksummed bytes.

Output shape: `schemas/assessment.schema.json`, which already rejects a result
with an empty `evidence` array.

## Implementation ladder

Deliberately incremental. The operational rule registry must be stable before
any ontology work begins.

```text
v0.2  FAIR rules in YAML/JSON        ← the registry; the hard part
  ↓
v0.2  JSON Schema output constraints ← already declared in v0.1
  ↓
v0.2  deterministic FAIR checks      ← the reference implementation
  ↓
v0.4  controlled vocabularies / ontology mappings
  ↓
v0.4  SHACL semantic validation
```

Not:

```text
start → build a giant FAIR ontology
```

An ontology built before the rule registry encodes guesses about which questions
matter. The registry is what tells you.

## Planned MCP surface

Added by the `fair-*` modes, never present in `structured`:

```text
run_fair_check(rule_id?)        run one rule, or the whole profile
get_fair_indicator(rule_id)     the canonical rule record
validate_identifier(value)      resolve a PID (the only networked check)
validate_metadata_schema(path)  validate against a declared schema
validate_vocabulary(value)      check a term against a controlled vocabulary
validate_shacl(path)            SHACL validation of RDF metadata
```

`validate_identifier` is the one check that touches the network. It must
therefore record the resolution attempt — endpoint, status, timestamp — as
evidence in `provenance.json`, because unlike every other check in the system it
is not reproducible from the dataset bytes alone.
