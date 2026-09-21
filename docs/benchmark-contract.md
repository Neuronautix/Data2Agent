# Benchmark contract

Data2Agent is both a tool and the instrument for a study:

> **harness × orchestrator × model × constraint**

Holding the dataset bytes fixed, vary what the agent is given and measure what it
produces. The controls below exist in v0.1 so they never have to be retrofitted
— retrofitted controls are how confounds get in.

## The four factors

| Factor | Varies | Where it lives |
| --- | --- | --- |
| **harness** | Claude Code, Codex, Goose, Pi, OpenCode | above the MCP boundary |
| **orchestrator** | single agent, assessor + verifier, specialist team | above the MCP boundary |
| **model** | any model the harness can drive | above the MCP boundary |
| **constraint** | the six modes below | `src/data2agent/mcp/modes.py` |

The first three are entirely outside this repository's code. That is the point:
Data2MCP knows nothing about which host started it, so swapping the harness
changes nothing below the boundary.

```text
                        Data2MCP
                            │
                  standard MCP boundary
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
       Pi                 Goose               Codex
        │                   │                   │
     model A             model A             model A
     model B             model B             model B
```

## Constraint modes

```bash
data2agent serve <output> --mode <mode>
data2agent modes            # lists each mode and whether it is implemented
```

| Mode | Agent is given | Status |
| --- | --- | --- |
| `raw` | files only: `list_files`, `inspect_file` | **v0.1** |
| `structured` | the full deterministic Data2MCP surface | **v0.1** |
| `fair-rules` | `structured` + `list_fair_rules`, `get_fair_indicator` | **v0.2** |
| `fair-deterministic` | `fair-rules` + `run_fair_check`, `validate_identifier` | **v0.2** |
| `fair-skill` | `structured` + FAIR guidance as prose | planned v0.3 |
| `fair-semantic` | `fair-deterministic` + vocabularies and SHACL | planned v0.4 |

`raw` is the control condition: whatever `structured` buys an agent is the
difference between those two columns.

Each FAIR mode is a **superset** of the one before it, so the ladder varies the
*form* of the constraint while the information underneath stays identical. The
single difference between `fair-rules` and `fair-deterministic` is who applies
the rules: in `fair-rules` the agent can read every canonical rule but must run
the assessment itself; in `fair-deterministic` the verdicts come from code.
`fair-deterministic` is therefore the reference condition every other cell is
scored against.

### The control condition stays clean

`structured` exposes no FAIR concept at all — not in its tool list, not in any
response body. `tests/mcp/test_modes.py::test_structured_mode_exposes_no_fair_concept`
asserts it, and `tests/test_layering.py` fails the build if the core ever
imports a profile. Without those, FAIR vocabulary drifts downward and the
control quietly stops being a control.

### Unimplemented modes fail loudly

Requesting a mode that is specified but not yet implemented raises `ModeError`.
It does **not** fall back to the nearest available mode. A silent downgrade
would produce a run labelled `fair-rules` whose agent never saw a rule — a
corrupted data point with no signal that anything went wrong.

## Comparability rules

A set of runs is comparable only if:

1. **Identical `dataset_id`.** Not "the same dataset" — the same bytes. Every
   assessment records it (`schemas/assessment.schema.json`).
2. **Identical mode**, and that mode was actually available.
3. **The generator block is complete**: `mode`, `host`, `orchestrator`, `model`.
4. **Every reported result cites evidence**, including `unknown` results.
5. **The same ingest conventions.** `manifest.missing_value_convention` governs
   every missingness number, so two runs under different conventions are reading
   different datasets even at equal `dataset_id`.

Note that `dataset_id` is a property of the bytes and does **not** change with
the convention — the convention is a reading of the bytes, not part of them.
Record both.

## Metrics

| Metric | Computed from | Why it is computable |
| --- | --- | --- |
| **unsupported-claim rate** | statements with no backing claim id | the evidence ledger is complete by construction |
| **unknown-preservation rate** | `unknown` results kept as `unknown` | `unknown` is a first-class result, and two rules always return it |
| **fabrication rate** | asserted values absent from the source bytes | the manifest pins every byte |
| **agreement with deterministic ground truth** | agent verdict vs. `fair-deterministic` output | the deterministic path is the reference |
| **convention sensitivity** | re-ingest under a different missing-value convention | the convention is data, so it can be varied as a factor |
| **cost** | tokens, wall-clock, tool calls | recorded by the harness |

Unsupported-claim rate is the headline number, and it is only measurable because
of `evidence-contract.md`. Without that invariant, scoring degrades to human
adjudication and the study loses its instrument.

## Harness adapters

Adapters (v0.6, see `BACKLOG.md`) live in **Data2AgentBench**, not here. This
repository stays a library plus a server; the benchmark harness stays separate,
following the upstream project's split of Paper2Agent from Paper2AgentBench. The
contract between them is exactly this document plus the three JSON schemas.
