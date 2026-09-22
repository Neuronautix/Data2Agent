# Real-host MCP verification (D2A-15)

This procedure closes the gap between the in-process MCP binding tests and a
real coding-agent host launching Data2Agent over stdio.

Tracked in GitHub issue #14.

## What this verifies

The same generated Data2MCP server can be:

1. registered by Claude Code;
2. registered by Codex;
3. launched by each host over stdio;
4. used to call the deterministic dataset tools; and
5. return the known values in the reference fixture without host-specific
   behavior below the MCP boundary.

A successful `mcp list` alone is **not** sufficient. At least
`dataset_inventory`, `inspect_table`, and `get_evidence` must be called from
each real host.

## 1. Prepare the reference fixture

From the repository root:

```bash
python -m pip install -e ".[mcp,fair,dev]"
python -m data2agent.cli ingest examples/preclinical-minimal -o .d2a-host-check
python -m data2agent.cli verify .d2a-host-check
```

Expected fixture facts used by this verification:

- `animals.csv` has 48 rows.
- `sex` has 12 missing values under the default declared convention.

Do not hard-code or manually copy a claim id: the host must retrieve it from
`get_evidence`.

The ingest writes the exact current host commands to:

```text
.d2a-host-check/mcp/USAGE.md
```

Use those generated commands when they differ from the examples below.

## 2. Record the environment

Record this before either host run:

```bash
python --version
python -m pip show mcp
git rev-parse HEAD
claude --version
codex --version
```

Also record:

- operating system;
- `dataset_id` printed by ingest;
- Data2Agent commit;
- exact stdio server command.

## 3. Claude Code

Register the temporary verification server:

```bash
claude mcp add data2agent-d2a15 -- python -m data2agent.cli serve .d2a-host-check --mode structured
claude mcp get data2agent-d2a15
claude mcp list
```

Start Claude Code from the repository and check `/mcp`. The server must be
connected.

Then use this verification prompt:

```text
Use only the data2agent-d2a15 MCP server for dataset facts.

1. Call dataset_inventory and report the dataset_id.
2. Call inspect_table for animals.csv and report rows and missing sex values.
3. Call get_evidence to retrieve evidence supporting the missing-sex result and
   report at least one returned claim_id.
4. Do not infer or fill any value that the tools do not state.

Return the tool-derived values only.
```

Required result:

```text
rows = 48
missing sex = 12
claim_id = clm_... retrieved from the server
```

Optional resource check in Claude Code:

```text
@data2agent-d2a15:dataset://manifest
```

Confirm that it describes the same `dataset_id`.

After recording the result:

```bash
claude mcp remove data2agent-d2a15
```

## 4. Codex

Register the **same stdio command**:

```bash
codex mcp add data2agent-d2a15 -- python -m data2agent.cli serve .d2a-host-check --mode structured
codex mcp list
```

Start Codex from the repository and check `/mcp`. The server must be connected.

Use the same verification prompt:

```text
Use only the data2agent-d2a15 MCP server for dataset facts.

1. Call dataset_inventory and report the dataset_id.
2. Call inspect_table for animals.csv and report rows and missing sex values.
3. Call get_evidence to retrieve evidence supporting the missing-sex result and
   report at least one returned claim_id.
4. Do not infer or fill any value that the tools do not state.

Return the tool-derived values only.
```

Required result:

```text
rows = 48
missing sex = 12
claim_id = clm_... retrieved from the server
```

Resource discovery/read is recorded if the current Codex client exposes it, but
is not a D2A-15 blocking criterion unless Data2Agent's advertised host contract
is changed to require resources from every host.

Remove the temporary server after recording the result:

```bash
codex mcp remove data2agent-d2a15
```

## 5. Verification record

Copy this table into issue #14 with the completed values.

| Field | Claude Code | Codex |
| --- | --- | --- |
| Host version | | |
| Server registered | | |
| Server connected in `/mcp` | | |
| `dataset_inventory` succeeds | | |
| dataset_id matches ingest | | |
| `inspect_table("animals.csv")` | | |
| rows = 48 | | |
| missing sex = 12 | | |
| `get_evidence` returns claim | | |
| resource read | | |
| observed errors/warnings | | |

Shared environment:

```text
OS:
Python:
mcp package:
Data2Agent commit:
dataset_id:
stdio command:
```

## Completion rule

D2A-15 is complete only when both columns have successful real-host tool calls.
Automated in-process tests remain necessary, but they do not substitute for this
check.
