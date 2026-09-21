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
| `fair-skill` | `structured` + FAIR guidance as prose | planned v0.2 |
| `fair-rules` | `structured` + the machine-readable rule registry | planned v0.2 |
| `fair-deterministic` | `structured` + deterministic FAIR check tools | planned v0.2 |
| `fair-semantic` | `fair-deterministic` + vocabularies and SHACL | planned v0.4 |

`raw` is the control condition: whatever `structured` buys an agent is the
difference between these two columns.

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

## Metrics

| Metric | Computed from | Why it is computable |
| --- | --- | --- |
| **unsupported-claim rate** | statements with no backing claim id | the evidence ledger is complete by construction |
| **unknown-preservation rate** | `unknown` results kept as `unknown` | `unknown` is a first-class result in the schema |
| **fabrication rate** | asserted values absent from the source bytes | the manifest pins every byte |
| **agreement with deterministic ground truth** | agent verdict vs. `fair-deterministic` output | the deterministic path is the reference |
| **cost** | tokens, wall-clock, tool calls | recorded by the harness |

Unsupported-claim rate is the headline number, and it is only measurable because
of `evidence-contract.md`. Without that invariant, scoring degrades to human
adjudication and the study loses its instrument.

## Harness adapters

Adapters (v0.6, see `BACKLOG.md`) live in **Data2AgentBench**, not here. This
repository stays a library plus a server; the benchmark harness stays separate,
following the upstream project's split of Paper2Agent from Paper2AgentBench. The
contract between them is exactly this document plus the three JSON schemas.
