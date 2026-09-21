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

**Status: implemented.** `src/data2agent/profiles/fair/` ships 12 canonical
rules, deterministic implementations for 10 of them, and the `fair-rules` and
`fair-deterministic` modes. The remaining two rules stay in the registry and
always return `unknown` — see "Rules this version cannot run" below.

## The rules

| Rule | Principle | Asks | Status |
| --- | --- | --- | --- |
| `F1-PID-METADATA` | F1 | a persistent identifier appears in dataset metadata | implemented |
| `F1-PID-RESOLVABLE` | F1 | that identifier actually resolves | **unknown** — needs the network |
| `F2-METADATA-PRESENT` | F2 | the data is described by metadata at all | implemented |
| `F3-METADATA-LINKS-DATA` | F3 | metadata names the files it describes | implemented |
| `F4-METADATA-MACHINE-READABLE` | F4 | some metadata is structured, not prose alone | implemented |
| `A1-RETRIEVAL-PROTOCOL` | A1 | data is retrievable over an open protocol | **unknown** — needs the published location |
| `I1-DATA-FORMATS-OPEN` | I1 | data files are in open, documented formats | implemented |
| `I2-VOCABULARY-REFERENCED` | I2 | metadata references a controlled vocabulary | implemented |
| `R1.1-LICENCE-DECLARED` | R1.1 | a usage licence is declared | implemented |
| `R1.2-PROVENANCE-DECLARED` | R1.2 | the data's origin is stated in its metadata | implemented |
| `R1.3-COMMUNITY-STANDARD` | R1.3 | metadata follows a community standard | implemented |
| `R1.3-MISSING-VALUES-DECLARED` | R1.3 | absence-like tokens such as `NA` are declared | implemented |

Run them:

```bash
data2agent assess ./dataset-agent           # writes assessment.json
data2agent assess ./dataset-agent --list    # the registry, without running it
data2agent assess ./dataset-agent --rule F1-PID-METADATA
```

### Rules this version cannot run

`F1-PID-RESOLVABLE` and `A1-RETRIEVAL-PROTOCOL` both depend on something a local
snapshot does not contain — a network round-trip, and the dataset's published
location. They are kept in the registry, marked `implemented: false`, and always
return `unknown` with the reason attached.

Dropping them would be easier and worse. A dropped rule leaves the denominator;
an `unknown` one stays visible and countable, which is exactly what
unknown-preservation rate measures.

### A rule that earns its place

`R1.3-MISSING-VALUES-DECLARED` is the sharpest of the twelve, because it is
actionable and it connects to a real decision in the core. Data2Agent resolves
`NA` to missing under a named convention so that *its own* numbers are
reproducible — but resolving under a built-in default means the **dataset**
never said what `NA` means, and every other tool will guess differently. The
rule reports that, names the tokens, and passes as soon as the dataset declares
its convention (for example via a Frictionless `missingValues`).

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

| Projection | Consumed by | Status |
| --- | --- | --- |
| a structured rule listing (`get_fair_indicator`, `list_fair_rules`) | `mode: fair-rules` | shipped |
| a deterministic check implementation (`run_fair_check`) | `mode: fair-deterministic` | shipped |
| a JSON Schema for the rule itself (`schemas/fair-rule.schema.json`) | authoring, CI | shipped |
| a JSON Schema for the result (`schemas/assessment.schema.json`) | output constraint | shipped |
| a Markdown Skill section | `mode: fair-skill` | v0.3 |
| SHACL shapes | `mode: fair-semantic` | v0.4 |

One canonical source with six projections is precisely what the constraint
experiment needs: the *content* is held constant while the *form of the
constraint* varies. Six independently written rule sets would confound form with
content and make the comparison meaningless.

## Non-negotiable rule semantics

Each of these is enforced in code, not merely documented. The loader rejects a
malformed rule at load time; the runner rejects a non-conforming outcome at
assessment time.

1. **`unknown` is a first-class result.** A rule that cannot return `unknown`
   will eventually invent an answer, so the **loader refuses to accept one**.
   `not_applicable` is distinct from `fail`, and must say why it does not apply.
2. **`inference_allowed: false` is the default.** Setting it true requires a
   written justification in the rule's `notes`, or the rule will not load. No
   rule in the shipped profile sets it.
3. **Every result carries evidence**, `unknown` and `not_applicable` included —
   an `unknown` must still say what was checked and came back empty. The runner
   raises otherwise.
4. **A rule may not read the dataset directly.** It consumes manifest fields,
   evidence claims, and metadata text served by the core, via `ProfileContext`.
   This keeps every FAIR verdict anchored to checksummed bytes, and
   `tests/test_layering.py` enforces the import direction.
5. **A `fail` or `unknown` needs a rationale** naming what was looked at, so a
   finding can be checked rather than taken on faith.

Output shape: `schemas/assessment.schema.json`, which rejects a result with an
empty `evidence` array. Rule shape: `schemas/fair-rule.schema.json`, which
rejects `allowed_results` without `unknown`, and `implemented: false` without a
reason. CI validates every shipped rule against it.

## Implementation ladder

Deliberately incremental. The operational rule registry must be stable before
any ontology work begins.

```text
v0.2  FAIR rules in YAML             ← shipped: 12 canonical rules
  ↓
v0.2  JSON Schema output constraints ← shipped: rule + assessment schemas
  ↓
v0.2  deterministic FAIR checks      ← shipped: 10 implementations
  ↓
v0.3  prose projection               ← the fair-skill mode
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

## MCP surface

Added by the `fair-*` modes, never present in `structured` —
`tests/mcp/test_modes.py::test_structured_mode_exposes_no_fair_concept` asserts
the control condition stays clean:

```text
list_fair_rules()               the registry: id, principle, question, status
get_fair_indicator(rule_id)     one canonical rule record, exactly as authored
run_fair_check(rule_id?)        run one rule, or the whole profile
validate_identifier(value)      syntax check against the scheme; no network call
validate_metadata_schema(path)  planned: validate against a declared schema
validate_vocabulary(value)      planned v0.4: a term against a vocabulary
validate_shacl(path)            planned v0.4: SHACL over RDF metadata
```

`list_fair_rules` and `get_fair_indicator` are available from `fair-rules`
upward; `run_fair_check` and `validate_identifier` only from
`fair-deterministic`. That is the whole difference between the two conditions:
in `fair-rules` the agent can read every rule but must apply it itself.

`validate_identifier` checks **syntax only** — including the ISO 7064 check
digit for an ORCID — and returns `resolves: null` with
`resolution_attempted: false`. A syntactically valid identifier is not a
resolvable one, and the response says so rather than leaving the distinction to
the reader.

Network resolution remains unimplemented. When it arrives it must record the
attempt — endpoint, status, timestamp — as evidence in `provenance.json`,
because unlike every other check in the system it is not reproducible from the
dataset bytes alone.
