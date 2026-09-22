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
| Host version | 2.1.278 | **not installed** |
| Server registered | yes | not attempted |
| Server connected in `/mcp` | yes, 8 tools | not attempted |
| `dataset_inventory` succeeds | yes | not attempted |
| dataset_id matches ingest | yes | not attempted |
| `inspect_table("animals.csv")` | yes | not attempted |
| rows = 48 | 48 | not attempted |
| missing sex = 12 | 12 (12 empty, 0 sentinel) | not attempted |
| `get_evidence` returns claim | `clm_2ff8a77fb78e1d95` | not attempted |
| resource read | not exercised | not attempted |
| observed errors/warnings | none once registered from WSL; see finding 1 | n/a |

Run date: 2026-09-22. Claude Code column complete; Codex column blocked.

The returned claim was checked against the ledger rather than taken on trust:

```text
claim_id  clm_2ff8a77fb78e1d95
claim     'sex' is missing for 12 of 48 row(s) in 'animals.csv'
          (12 empty, 0 resolved from tokens by the 'default-sentinels' convention)
checks    table.missing-value-count, table.missing-empty-count,
          table.missing-sentinel-count, convention.missing-values
file      animals.csv sha256 39c49a37...4b0308 -- matches manifest
```

It is the only claim in the ledger stating `sex ... 12 of 48`, and the host also
returned the file digest, which matches the manifest. The id was retrieved from
the server, not supplied to the host.

Shared environment:

```text
OS:                WSL2 Ubuntu on Windows 11 10.0.26200
Python:            3.12.3 (WSL; a separate Windows 3.12.6 install also exists)
mcp package:       2.2.0
Data2Agent commit: e634442
dataset_id:        sha256:23233f557fec10d951a2185d38efddcef231aa2c35ad32d2c1c0ed59ab4de62c
stdio command:     /home/dhuzard/.venv-d2a/bin/python -m data2agent.cli serve                    /mnt/c/Users/damie/Documents/GitHub/Data2Agent/.d2a-host-check-wsl                    --mode structured
```

## 6. Findings from the first real run

### Finding 1 -- the generated server command is environment-bound, silently

Section 1 says the ingest "writes the exact current host commands". They are exact
for the **ingesting** environment, not the **hosting** one, and nothing in
`mcp/server.json` or `mcp/USAGE.md` says so.

On this machine the repository lives on the Windows filesystem while Claude Code
is installed only inside WSL. Ingesting with Windows Python emitted:

```json
"command": "C:\Python312\python.exe"
```

which cannot resolve in a WSL host. The failure mode is poor: registration
succeeds, `mcp list` shows the server, and only the connection fails.

Re-running the ingest inside WSL produced a WSL-native command and the server
connected immediately. So the procedure works, but section 1 must say that the
ingest has to run in the same environment as the host that will launch it.

Worth fixing in the tool rather than only in this document: `server.json` could
record the interpreter and platform it was generated for, so a mismatch is
detectable instead of silent.

### Finding 2 -- `dataset_id` is platform-independent in practice

The same fixture ingested under Windows Python 3.12.6 and WSL Python 3.12.3
produced an identical `dataset_id`:

```text
sha256:23233f557fec10d951a2185d38efddcef231aa2c35ad32d2c1c0ed59ab4de62c
```

`docs/data-contract.md` claims independence from "filesystem walk order, machine,
clock and absolute path". This demonstrates it across two operating systems and
two Python patch versions, which the determinism tests do not cover.

### Finding 3 -- Codex blocks completion

Codex is installed in neither WSL nor Windows on this machine, so section 4 could
not be attempted. Under the completion rule below, D2A-15 stays open and the v0.1
acceptance list stays 7/8.

## Completion rule

D2A-15 is complete only when both columns have successful real-host tool calls.
Automated in-process tests remain necessary, but they do not substitute for this
check.
